from pathlib import Path
import asyncio
import time
from typing import Dict, Optional, Tuple, List
import concurrent
import logging
from blspy import ExtendedPrivateKey
from src.util.merkle_set import (
    confirm_included_already_hashed,
    confirm_not_included_already_hashed,
    MerkleSet,
)
from src.protocols import wallet_protocol
from src.consensus.constants import constants as consensus_constants
from src.server.server import ChiaServer
from src.server.outbound_message import OutboundMessage, NodeType, Message, Delivery
from src.util.ints import uint32, uint64
from src.types.sized_bytes import bytes32
from src.util.api_decorators import api_request
from src.wallet.wallet import Wallet
from src.wallet.wallet_state_manager import WalletStateManager
from src.wallet.block_record import BlockRecord
from src.types.header_block import HeaderBlock
from src.types.full_block import FullBlock
from src.types.hashable.coin import Coin, hash_coin_list
from src.full_node.blockchain import ReceiveBlockResult


class WalletNode:
    private_key: ExtendedPrivateKey
    key_config: Dict
    config: Dict
    server: Optional[ChiaServer]
    wallet_state_manager: WalletStateManager
    log: logging.Logger
    wallet: Wallet
    cached_blocks: Dict[bytes32, Tuple[BlockRecord, HeaderBlock]]
    cached_removals: Dict[bytes32, List[bytes32]]
    cached_additions: Dict[bytes32, List[Coin]]
    proof_hashes: List[Tuple[bytes32, Optional[uint64]]]
    header_hashes: List[bytes32]
    potential_blocks_received: Dict[uint32, asyncio.Event]
    potential_header_hashes: Dict[uint32, bytes32]
    constants: Dict
    short_sync_threshold: int
    sync_mode: bool
    _shut_down: bool

    @staticmethod
    async def create(
        config: Dict,
        key_config: Dict,
        name: str = None,
        db_path=None,
        override_constants: Dict = {},
    ):
        self = WalletNode()
        self.config = config
        self.key_config = key_config
        sk_hex = self.key_config["wallet_sk"]
        self.private_key = ExtendedPrivateKey.from_bytes(bytes.fromhex(sk_hex))
        self.constants = consensus_constants.copy()
        for key, value in override_constants.items():
            self.constants[key] = value
        if name:
            self.log = logging.getLogger(name)
        else:
            self.log = logging.getLogger(__name__)

        pub_hex = self.private_key.get_public_key().serialize().hex()
        if not db_path:
            path = Path(f"wallet_db_{pub_hex}.db")
        else:
            path = db_path

        self.wallet_state_manager = await WalletStateManager.create(
            config, path, override_constants=self.constants,
        )
        self.wallet = await Wallet.create(config, key_config, self.wallet_state_manager)

        # Normal operation data
        self.cached_blocks = {}
        self.cached_removals = {}
        self.cached_additions = {}

        # Sync data
        self.sync_mode = False
        self._shut_down = False
        self.proof_hashes = []
        self.header_hashes = []
        self.short_sync_threshold = 10
        self.potential_blocks_received = {}
        self.potential_header_hashes = {}

        self.server = None

        return self

    def set_server(self, server: ChiaServer):
        self.server = server
        self.wallet.set_server(server)

    def _shutdown(self):
        self._shut_down = True

    async def _sync(self):
        """
        Wallet has fallen far behind (or is starting up for the first time), and must be synced
        up to the tip of the blockchain
        """
        # 1. Get all header hashes
        self.header_hashes = []
        self.proof_hashes = []
        self.potential_header_hashes = {}
        genesis = FullBlock.from_bytes(self.constants["GENESIS_BLOCK"])
        genesis_challenge = genesis.proof_of_space.challenge_hash
        request_header_hashes = wallet_protocol.RequestAllHeaderHashesAfter(
            uint32(0), genesis_challenge
        )
        yield OutboundMessage(
            NodeType.FULL_NODE,
            Message("request_all_header_hashes_after", request_header_hashes),
            Delivery.RESPOND,
        )
        timeout = 100
        start_wait = time.time()
        while time.time() - start_wait < timeout:
            if self._shut_down:
                return
            if len(self.header_hashes) > 0:
                break
            await asyncio.sleep(0.5)
        if len(self.header_hashes) == 0:
            raise TimeoutError("Took too long to fetch header hashes.")

        # 2. Find fork point
        fork_point_height: uint32 = self.wallet_state_manager.find_fork_point(
            self.header_hashes
        )
        fork_point_hash: bytes32 = self.header_hashes[fork_point_height]
        self.log.info(f"Fork point: {fork_point_hash} at height {fork_point_height}")
        tip_height = (
            len(self.header_hashes) - 5
            if len(self.header_hashes) > 5
            else len(self.header_hashes)
        )

        header_validate_start_height: uint32
        if self.config["starting_height"] == 0:
            header_validate_start_height = fork_point_height
        else:
            # Request all proof hashes
            request_proof_hashes = wallet_protocol.RequestAllProofHashes()
            yield OutboundMessage(
                NodeType.FULL_NODE,
                Message("request_all_proof_hashes", request_proof_hashes),
                Delivery.RESPOND,
            )
            start_wait = time.time()
            while time.time() - start_wait < timeout:
                if self._shut_down:
                    return
                if len(self.proof_hashes) > 0:
                    break
                await asyncio.sleep(0.5)
            if len(self.proof_hashes) == 0:
                raise TimeoutError("Took too long to fetch proof hashes.")

            # TODO(mariano): Validate weight
            # - Request headers for a random subset
            # - Verify those proofs

            weight = self.wallet_state_manager.block_records[fork_point_hash].weight
            header_validate_start_height = max(
                fork_point_height, self.config["starting_height"] - 1
            )
            if fork_point_height == 0:
                difficulty = self.constants["STARTING_DIFFICULTY"]
            else:
                fork_point_parent_hash = self.wallet_state_manager.block_records[
                    fork_point_hash
                ].prev_header_hash
                fork_point_parent_weight = self.wallet_state_manager.block_records[
                    fork_point_parent_hash
                ]
                difficulty = uint64(weight - fork_point_parent_weight)
            for height in range(fork_point_height + 1, header_validate_start_height):
                _, difficulty_change = self.proof_hashes[height]
                if difficulty_change is not None:
                    difficulty = difficulty_change
                weight += difficulty
                block_record = BlockRecord(
                    self.header_hashes[height],
                    self.header_hashes[height - 1],
                    uint32(height),
                    weight,
                    [],
                    [],
                )
                res = await self.wallet_state_manager.receive_block(block_record, None)

        # Download headers in batches, and verify them as they come in. We download a few batches ahead,
        # in case there are delays. TODO(mariano): optimize sync by pipelining
        for height in range(0, tip_height + 1):
            self.potential_blocks_received[uint32(height)] = asyncio.Event()
        last_request_time = float(0)
        highest_height_requested = uint32(0)
        request_made = False
        sleep_interval = 10

        for height_checkpoint in range(
            header_validate_start_height + 1, tip_height + 1, 1
        ):
            end_height = min(height_checkpoint + 1, tip_height + 1)

            total_time_slept = 0
            while True:
                if self._shut_down:
                    return
                if total_time_slept > timeout:
                    raise TimeoutError("Took too long to fetch blocks")

                # Request batches that we don't have yet
                for batch in range(0, self.config["num_sync_batches"]):
                    batch_start = uint32(height_checkpoint + batch)
                    batch_end = min(batch_start + 1, tip_height + 1)

                    if batch_start > tip_height:
                        # We have asked for all blocks
                        break

                    blocks_missing = any(
                        [
                            not (self.potential_blocks_received[uint32(h)]).is_set()
                            for h in range(batch_start, batch_end)
                        ]
                    )
                    if (
                        time.time() - last_request_time > sleep_interval
                        and blocks_missing
                    ) or (batch_end - 1) > highest_height_requested:
                        # If we are missing blocks in this batch, and we haven't made a request in a while,
                        # Make a request for this batch. Also, if we have never requested this batch, make
                        # the request
                        self.log.info(f"Requesting sync header {batch_start}")
                        if batch_end - 1 > highest_height_requested:
                            highest_height_requested = uint32(batch_end - 1)
                        request_made = True
                        request_header = wallet_protocol.RequestHeader(
                            batch_start, self.header_hashes[batch_start],
                        )
                        yield OutboundMessage(
                            NodeType.FULL_NODE,
                            Message("request_header", request_header),
                            Delivery.RANDOM,
                        )
                if request_made:
                    # Reset the timer for requests, so we don't overload other peers with requests
                    last_request_time = time.time()
                    request_made = False

                awaitables = [
                    self.potential_blocks_received[uint32(height)].wait()
                    for height in range(height_checkpoint, end_height)
                ]
                future = asyncio.gather(*awaitables, return_exceptions=True)
                try:
                    await asyncio.wait_for(future, timeout=sleep_interval)
                    break
                except concurrent.futures.TimeoutError:
                    try:
                        await future
                    except asyncio.CancelledError:
                        pass
                    total_time_slept += sleep_interval
                    self.log.info("Did not receive desired headers")

            # Verifies this batch, which we are guaranteed to have (since we broke from the above loop)
            for height in range(height_checkpoint, end_height):
                hh = self.potential_header_hashes[height]
                block_record, header_block = self.cached_blocks[hh]

                res = await self.wallet_state_manager.receive_block(
                    block_record, header_block
                )
                if (
                    res == ReceiveBlockResult.INVALID_BLOCK
                    or res == ReceiveBlockResult.DISCONNECTED_BLOCK
                ):
                    raise RuntimeError(
                        f"Invalid block header {block_record.header_hash}"
                    )
        self.log.info(
            f"Finished sync process up to height {max(self.wallet_state_manager.height_to_hash.keys())}"
        )

    async def _block_finished(
        self, block_record: BlockRecord, header_block: HeaderBlock
    ):
        if self.sync_mode:
            print("Got sync mode block at heeight", block_record.height)
            self.potential_blocks_received[uint32(block_record.height)].set()
            self.potential_header_hashes[block_record.height] = block_record.header_hash
            self.cached_blocks[block_record.header_hash] = (block_record, header_block)
            return
        # 1. If disconnected and close, get parent header and return
        lca = self.wallet_state_manager.block_records[self.wallet_state_manager.lca]
        if block_record.prev_header_hash in self.wallet_state_manager.block_records:
            # We have completed a block that we can add to chain, so add it.
            res = await self.wallet_state_manager.receive_block(
                block_record, header_block
            )
            if res == ReceiveBlockResult.DISCONNECTED_BLOCK:
                self.log.error("Attempted to add disconnected block")
                return
            elif res == ReceiveBlockResult.INVALID_BLOCK:
                self.log.error("Attempted to add invalid block")
                return
            elif res == ReceiveBlockResult.ALREADY_HAVE_BLOCK:
                return
            else:
                # If we have the next block available, add it
                if block_record.header_hash in self.cached_blocks:
                    new_br, new_hb = self.cached_blocks[block_record.header_hash]
                    async for msg in self._block_finished(new_br, new_hb):
                        yield msg
            if res == ReceiveBlockResult.ADDED_TO_HEAD:
                self.log.info(
                    f"Updated LCA to {block_record.prev_header_hash} at height {block_record.height}"
                )
                # Removes outdated cached blocks if we're not syncing
                if not self.sync_mode:
                    for header_hash in self.cached_blocks:
                        if (
                            block_record.height
                            - self.cached_blocks[header_hash][0].height
                            > 100
                        ):
                            del self.cached_blocks[header_hash]
                            if header_hash in self.cached_additions:
                                del self.cached_additions[header_hash]
                            if header_hash in self.cached_removals:
                                del self.cached_removals[header_hash]
        else:
            if block_record.height - lca.height < self.short_sync_threshold:
                # We have completed a block that is in the near future, so cache it, and fetch parent
                self.cached_blocks[block_record.prev_header_hash] = (
                    block_record,
                    header_block,
                )

                header_request = wallet_protocol.RequestHeader(
                    uint32(block_record.height - 1), block_record.prev_header_hash,
                )
                yield OutboundMessage(
                    NodeType.FULL_NODE,
                    Message("request_header", header_request),
                    Delivery.RESPOND,
                )
                return
            self.log.warning("Block too far ahead in the future, should never get here")
            return

    @api_request
    async def transaction_ack(self, ack: wallet_protocol.TransactionAck):
        if ack.status:
            await self.wallet_state_manager.remove_from_queue(ack.txid)
            self.log.info(f"SpendBundle has been received by the FullNode. id: {id}")
        else:
            self.log.info(f"SpendBundle has been rejected by the FullNode. id: {id}")

    @api_request
    async def respond_all_proof_hashes(
        self, response: wallet_protocol.RespondAllProofHashes
    ):
        if not self.sync_mode:
            self.log.warning("Receiving proof hashes while not syncing.")
            return
        self.proof_hashes = response.hashes

    @api_request
    async def respond_all_header_hashes_after(
        self, response: wallet_protocol.RespondAllHeaderHashesAfter
    ):
        if not self.sync_mode:
            self.log.warning("Receiving header hashes while not syncing.")
            return
        self.header_hashes = response.hashes

    @api_request
    async def reject_all_header_hashes_after_request(
        self, response: wallet_protocol.RejectAllHeaderHashesAfterRequest
    ):
        # TODO(mariano): retry
        self.log.error("All header hashes after request rejected")
        pass

    @api_request
    async def new_lca(self, request: wallet_protocol.NewLCA):
        if self.sync_mode:
            return
        # If already seen LCA, ignore.
        if request.lca_hash in self.wallet_state_manager.block_records:
            return

        lca = self.wallet_state_manager.block_records[self.wallet_state_manager.lca]
        # If it's not the heaviest chain, ignore.
        if request.weight < lca.weight:
            return

        if int(request.height) - int(lca.height) > self.short_sync_threshold:
            try:
                # Performs sync, and catch exceptions so we don't close the connection
                self.sync_mode = True
                async for ret_msg in self._sync():
                    yield ret_msg
            except asyncio.CancelledError:
                self.log.error("Syncing failed, CancelledError")
            except BaseException as e:
                self.log.error(f"Error {type(e)}{e} with syncing")
            self.sync_mode = False
        else:
            header_request = wallet_protocol.RequestHeader(
                uint32(request.height), request.lca_hash
            )
            yield OutboundMessage(
                NodeType.FULL_NODE,
                Message("request_header", header_request),
                Delivery.RESPOND,
            )

    @api_request
    async def respond_header(self, response: wallet_protocol.RespondHeader):
        block = response.header_block
        # If we already have, return
        if block.header_hash in self.wallet_state_manager.block_records:
            return

        block_record = BlockRecord(
            block.header_hash,
            block.prev_header_hash,
            block.height,
            block.weight,
            [],
            [],
        )
        finish_block = True

        # If we have transactions, fetch adds/deletes
        if response.transactions_filter is not None:
            # Caches the block so we can finalize it when additions and removals arrive
            self.cached_blocks[block.header_hash] = (block_record, block)
            (
                additions,
                removals,
            ) = await self.wallet_state_manager.get_filter_additions_removals(
                response.transactions_filter
            )
            if len(additions) > 0:
                finish_block = False
                request_a = wallet_protocol.RequestAdditions(
                    block.height, block.header_hash, additions
                )
                yield OutboundMessage(
                    NodeType.FULL_NODE,
                    Message("request_additions", request_a),
                    Delivery.RESPOND,
                )
            if len(removals) > 0:
                finish_block = False
                request_r = wallet_protocol.RequestRemovals(
                    block.height, block.header_hash, removals
                )
                yield OutboundMessage(
                    NodeType.FULL_NODE,
                    Message("request_removals", request_r),
                    Delivery.RESPOND,
                )
        if finish_block:
            # If we don't have any transactions in filter, don't fetch, and finish the block
            async for msg in self._block_finished(block_record, block):
                yield msg

    @api_request
    async def reject_header_request(
        self, response: wallet_protocol.RejectHeaderRequest
    ):
        # TODO(mariano): implement
        self.log.error("Header request rejected")

    @api_request
    async def respond_removals(self, response: wallet_protocol.RespondRemovals):
        if response.header_hash not in self.cached_blocks:
            self.log.warning("Do not have header for removals")
            return
        block_record, header_block = self.cached_blocks[response.header_hash]
        assert response.height == block_record.height

        removals: List[bytes32]
        if response.proofs is None:
            # Find our removals
            all_coins: List[Coin] = []
            for coin_name, coin in response.coins:
                if coin is not None:
                    all_coins.append(coin)
            removals = [
                c.name()
                for c in await self.wallet_state_manager.get_relevant_removals(
                    all_coins
                )
            ]

            # Verify removals root
            removals_merkle_set = MerkleSet()
            for coin in removals:
                if coin is not None:
                    removals_merkle_set.add_already_hashed(coin.name())
            removals_root = removals_merkle_set.get_root()
            if header_block.header.data.removals_root != removals_root:
                return
        else:
            removals = []
            assert len(response.coins) == len(response.proofs)
            for i in range(len(response.coins)):
                # Coins are in the same order as proofs
                assert response.coins[i][0] == response.proofs[i][0]
                coin = response.coins[i][1]
                if coin is None:
                    assert confirm_not_included_already_hashed(
                        header_block.header.data.removals_root,
                        response.coins[i][0],
                        response.proofs[i][1],
                    )
                else:
                    assert response.coins[i][0] == coin.name
                    assert confirm_included_already_hashed(
                        header_block.header.data.removals_root,
                        coin.name(),
                        response.proofs[i][1],
                    )
                    removals.append(response.coins[i][0])
        additions = self.cached_additions.get(response.header_hash, [])
        new_br = BlockRecord(
            block_record.header_hash,
            block_record.prev_header_hash,
            block_record.height,
            block_record.weight,
            additions,
            removals,
        )
        self.cached_blocks[response.header_hash] = (new_br, header_block)
        self.cached_removals[response.header_hash] = removals

        if response.header_hash in self.cached_additions:
            # We have collected all three things: header, additions, and removals. Can proceed.
            # Otherwise, we wait for the additions to arrive
            async for msg in self._block_finished(new_br, header_block):
                yield msg

    @api_request
    async def reject_removals_request(
        self, response: wallet_protocol.RejectRemovalsRequest
    ):
        # TODO(mariano): implement
        self.log.error("Removals request rejected")

    @api_request
    async def respond_additions(self, response: wallet_protocol.RespondAdditions):
        if response.header_hash not in self.cached_blocks:
            self.log.warning("Do not have header for additions")
            return
        block_record, header_block = self.cached_blocks[response.header_hash]
        assert response.height == block_record.height

        additions: List[Coin]
        if response.proofs is None:
            # Find our removals
            all_coins: List[Coin] = []
            for puzzle_hash, coin_list_0 in response.coins:
                all_coins += coin_list_0
            additions = await self.wallet_state_manager.get_relevant_additions(
                all_coins
            )
            # Verify root
            additions_merkle_set = MerkleSet()

            # Addition Merkle set contains puzzlehash and hash of all coins with that puzzlehash
            for puzzle_hash, coins in response.coins:
                additions_merkle_set.add_already_hashed(puzzle_hash)
                additions_merkle_set.add_already_hashed(hash_coin_list(coins))

            additions_root = additions_merkle_set.get_root()
            if header_block.header.data.additions_root != additions_root:
                return
        else:
            additions = []
            assert len(response.coins) == len(response.proofs)
            for i in range(len(response.coins)):
                assert response.coins[i][0] == response.proofs[i][0]
                coin_list_1: List[Coin] = response.coins[i][1]
                puzzle_hash_proof: bytes32 = response.proofs[i][1]
                coin_list_proof: Optional[bytes32] = response.proofs[i][2]
                if len(coin_list_1) == 0:
                    # Verify exclusion proof for puzzle hash
                    assert confirm_not_included_already_hashed(
                        header_block.header.data.additions_root,
                        response.coins[i][0],
                        puzzle_hash_proof,
                    )
                else:
                    # Verify inclusion proof for puzzle hash
                    assert confirm_included_already_hashed(
                        header_block.header.data.additions_root,
                        response.coins[i][0],
                        puzzle_hash_proof,
                    )
                    # Verify inclusion proof for coin list
                    assert confirm_included_already_hashed(
                        header_block.header.data.additions_root,
                        hash_coin_list(coin_list_1),
                        coin_list_proof,
                    )
                    for coin in coin_list_1:
                        assert coin.puzzle_hash == response.coins[i][0]
                    additions += coin_list_1
        removals = self.cached_removals.get(response.header_hash, [])
        new_br = BlockRecord(
            block_record.header_hash,
            block_record.prev_header_hash,
            block_record.height,
            block_record.weight,
            additions,
            removals,
        )
        self.cached_blocks[response.header_hash] = (new_br, header_block)
        self.cached_additions[response.header_hash] = additions

        if response.header_hash in self.cached_removals:
            # We have collected all three things: header, additions, and removals. Can proceed.
            # Otherwise, we wait for the removals to arrive
            async for msg in self._block_finished(new_br, header_block):
                yield msg

    @api_request
    async def reject_additions_request(
        self, response: wallet_protocol.RejectAdditionsRequest
    ):
        # TODO(mariano): implement
        self.log.error("Additions request rejected")
