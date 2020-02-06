import collections
import logging
import multiprocessing
import time
from enum import Enum
from typing import Dict, List, Optional, Tuple
import asyncio
import concurrent
import blspy

from src.consensus.block_rewards import calculate_block_reward
from src.consensus.constants import constants as consensus_constants
from src.consensus.pot_iterations import (
    calculate_ips_from_iterations,
    calculate_iterations_quality,
)
from src.store import FullNodeStore

from src.types.full_block import FullBlock, additions_for_npc
from src.types.hashable.Coin import Coin
from src.types.hashable.Unspent import Unspent
from src.types.header_block import HeaderBlock, SmallHeaderBlock
from src.types.sized_bytes import bytes32
from src.unspent_store import UnspentStore
from src.util.ConsensusError import Err
from src.util.blockchain_check_conditions import blockchain_check_conditions_dict
from src.util.consensus import hash_key_pairs_for_conditions_dict
from src.util.mempool_check_conditions import get_name_puzzle_conditions
from src.util.errors import BlockNotInBlockchain, InvalidGenesisBlock
from src.util.ints import uint32, uint64

log = logging.getLogger(__name__)


class ReceiveBlockResult(Enum):
    """
    When Blockchain.receive_block(b) is called, one of these results is returned,
    showing whether the block was added to the chain (extending a head or not),
    and if not, why it was not added.
    """

    ADDED_TO_HEAD = 1  # Added to one of the heads, this block is now a new head
    ADDED_AS_ORPHAN = 2  # Added as an orphan/stale block (block that is not a head or ancestor of a head)
    INVALID_BLOCK = 3  # Block was not added because it was invalid
    ALREADY_HAVE_BLOCK = 4  # Block is already present in this blockchain
    DISCONNECTED_BLOCK = (
        5  # Block's parent (previous pointer) is not in this blockchain
    )


class Blockchain:
    # Allow passing in custom overrides for any consesus parameters
    constants: Dict
    # Tips of the blockchain
    tips: List[SmallHeaderBlock]
    # Least common ancestor of tips
    lca_block: SmallHeaderBlock
    # Defines the path from genesis to the lca
    height_to_hash: Dict[uint32, bytes32]
    # All headers (but not orphans) from genesis to the tip are guaranteed to be in header_blocks
    headers: Dict[bytes32, SmallHeaderBlock]
    # Process pool to verify blocks
    pool: concurrent.futures.ProcessPoolExecutor
    # Genesis block
    genesis: FullBlock
    # Unspent Store
    unspent_store: UnspentStore
    # Store
    store: FullNodeStore
    # Coinbase freeze period
    coinbase_freeze: int

    @staticmethod
    async def create(
        headers_input: Dict[str, SmallHeaderBlock],
        unspent_store: UnspentStore,
        store: FullNodeStore,
        override_constants: Dict = {},
    ):
        """
        Initializes a blockchain with the given header blocks, assuming they have all been
        validated. If no header_blocks are given, only the genesis block is added.
        Uses the genesis block given in override_constants, or as a fallback,
        in the consensus constants config.
        """
        self = Blockchain()
        self.constants = consensus_constants
        for key, value in override_constants.items():
            self.constants[key] = value
        self.tips = []
        self.height_to_hash = {}
        self.headers = {}

        self.unspent_store = unspent_store
        self.store = store

        self.genesis = FullBlock.from_bytes(self.constants["GENESIS_BLOCK"])
        self.coinbase_freeze = self.constants["COINBASE_FREEZE_PERIOD"]
        result, removed = await self.receive_block(self.genesis)
        if result != ReceiveBlockResult.ADDED_TO_HEAD:
            raise InvalidGenesisBlock()

        assert self.lca_block is not None
        if len(headers_input) > 0:
            self.headers = headers_input
            for _, header_block in self.headers.items():
                self.height_to_hash[header_block.height] = header_block.header_hash
                await self._reconsider_heads(header_block, False)
            assert (
                self.headers[self.height_to_hash[uint32(0)]].header.get_hash()
                == self.genesis.header_block.header_hash
            )
        if len(headers_input) > 1:
            assert (
                self.headers[self.height_to_hash[uint32(1)]].prev_header_hash
                == self.genesis.header_hash
            )
        return self

    def get_current_tips(self) -> List[SmallHeaderBlock]:
        """
        Return the heads.
        """
        return self.tips[:]

    def is_child_of_head(self, block: FullBlock):
        """
        True iff the block is the direct ancestor of a head.
        """
        for head in self.tips:
            if block.prev_header_hash == head.header_hash:
                return True
        return False

    def cointains_block(self, header_hash: bytes32):
        return header_hash in self.headers

    def get_header_hashes(self, tip_header_hash: bytes32) -> List[bytes32]:
        if tip_header_hash not in self.headers:
            raise ValueError("Invalid tip requested")

        curr = self.headers[tip_header_hash]
        ret_hashes = [tip_header_hash]
        while curr.height != 0:
            curr = self.headers[curr.prev_header_hash]
            ret_hashes.append(curr.header_hash)
        return list(reversed(ret_hashes))

    def get_header_hashes_by_height(
        self, heights: List[uint32], tip_header_hash: bytes32
    ) -> List[bytes32]:
        """
        Returns a list of header blocks, one for each height requested.
        """
        if len(heights) == 0:
            return []

        sorted_heights = sorted(
            [(height, index) for index, height in enumerate(heights)], reverse=True
        )

        curr_block: Optional[SmallHeaderBlock] = self.headers.get(tip_header_hash, None)

        if curr_block is None:
            raise BlockNotInBlockchain(
                f"Header hash {tip_header_hash} not present in chain."
            )
        headers: List[Tuple[int, SmallHeaderBlock]] = []
        for height, index in sorted_heights:
            if height > curr_block.height:
                raise ValueError("Height is not valid for tip {tip_header_hash}")
            while height < curr_block.height:
                curr_block = self.headers.get(curr_block.prev_header_hash, None)
                if curr_block is None:
                    raise ValueError(f"Do not have header {height}")
            headers.append((index, curr_block))

        # Return sorted by index (original order)
        return [b.header_hash for _, b in sorted(headers, key=lambda pair: pair[0])]

    def find_fork_point(self, alternate_chain: List[bytes32]) -> uint32:
        """
        Takes in an alternate blockchain (headers), and compares it to self. Returns the last header
        where both blockchains are equal.
        """
        lca: SmallHeaderBlock = self.lca_block

        if lca.height >= len(alternate_chain) - 1:
            raise ValueError("Alternate chain is shorter")
        low: uint32 = uint32(0)
        high = lca.height
        while low + 1 < high:
            mid = (low + high) // 2
            if self.height_to_hash[uint32(mid)] != alternate_chain[mid]:
                high = mid
            else:
                low = mid
        if low == high and low == 0:
            assert self.height_to_hash[uint32(0)] == alternate_chain[0]
            return uint32(0)
        assert low + 1 == high
        if self.height_to_hash[uint32(low)] == alternate_chain[low]:
            if self.height_to_hash[uint32(high)] == alternate_chain[high]:
                return high
            else:
                return low
        elif low > 0:
            assert self.height_to_hash[uint32(low - 1)] == alternate_chain[low - 1]
            return uint32(low - 1)
        else:
            raise ValueError("Invalid genesis block")

    def get_next_difficulty(self, header_hash: bytes32) -> uint64:
        """
        Returns the difficulty of the next block that extends onto header_hash.
        Used to calculate the number of iterations.
        """
        block: SmallHeaderBlock = self.headers[header_hash]

        next_height: uint32 = uint32(block.height + 1)
        if next_height < self.constants["DIFFICULTY_EPOCH"]:
            # We are in the first epoch
            return uint64(self.constants["DIFFICULTY_STARTING"])

        # Epochs are diffined as intervals of DIFFICULTY_EPOCH blocks, inclusive and indexed at 0.
        # For example, [0-2047], [2048-4095], etc. The difficulty changes DIFFICULTY_DELAY into the
        # epoch, as opposed to the first block (as in Bitcoin).
        elif (
            next_height % self.constants["DIFFICULTY_EPOCH"]
            != self.constants["DIFFICULTY_DELAY"]
        ):
            # Not at a point where difficulty would change
            prev_block: SmallHeaderBlock = self.headers[block.prev_header_hash]
            assert block.challenge is not None
            assert prev_block is not None and prev_block.challenge is not None
            if prev_block is None:
                raise Exception("Previous block is invalid.")
            return uint64(
                block.challenge.total_weight - prev_block.challenge.total_weight
            )

        #       old diff                  curr diff       new diff
        # ----------|-----|----------------------|-----|-----...
        #           h1    h2                     h3   i-1
        # Height1 is the last block 2 epochs ago, so we can include the time to mine 1st block in previous epoch
        height1 = uint32(
            next_height
            - self.constants["DIFFICULTY_EPOCH"]
            - self.constants["DIFFICULTY_DELAY"]
            - 1
        )
        # Height2 is the DIFFICULTY DELAYth block in the previous epoch
        height2 = uint32(next_height - self.constants["DIFFICULTY_EPOCH"] - 1)
        # Height3 is the last block in the previous epoch
        height3 = uint32(next_height - self.constants["DIFFICULTY_DELAY"] - 1)

        # h1 to h2 timestamps are mined on previous difficulty, while  and h2 to h3 timestamps are mined on the
        # current difficulty

        block1, block2, block3 = None, None, None
        if block not in self.get_current_tips() or height3 not in self.height_to_hash:
            # This means we are either on a fork, or on one of the chains, but after the LCA,
            # so we manually backtrack.
            curr: Optional[SmallHeaderBlock] = block
            assert curr is not None
            while (
                curr.height not in self.height_to_hash
                or self.height_to_hash[curr.height] != curr.header_hash
            ):
                if curr.height == height1:
                    block1 = curr
                elif curr.height == height2:
                    block2 = curr
                elif curr.height == height3:
                    block3 = curr
                curr = self.headers.get(curr.prev_header_hash, None)
                assert curr is not None
        # Once we are before the fork point (and before the LCA), we can use the height_to_hash map
        if not block1 and height1 >= 0:
            # height1 could be -1, for the first difficulty calculation
            block1 = self.headers[self.height_to_hash[height1]]
        if not block2:
            block2 = self.headers[self.height_to_hash[height2]]
        if not block3:
            block3 = self.headers[self.height_to_hash[height3]]
        assert block2 is not None and block3 is not None

        # Current difficulty parameter (diff of block h = i - 1)
        Tc = self.get_next_difficulty(block.prev_header_hash)

        # Previous difficulty parameter (diff of block h = i - 2048 - 1)
        Tp = self.get_next_difficulty(block2.prev_header_hash)
        if block1:
            timestamp1 = block1.header.data.timestamp  # i - 512 - 1
        else:
            # In the case of height == -1, there is no timestamp here, so assume the genesis block
            # took constants["BLOCK_TIME_TARGET"] seconds to mine.
            genesis = self.headers[self.height_to_hash[uint32(0)]]
            timestamp1 = (
                genesis.header.data.timestamp - self.constants["BLOCK_TIME_TARGET"]
            )
        timestamp2 = block2.header.data.timestamp  # i - 2048 + 512 - 1
        timestamp3 = block3.header.data.timestamp  # i - 512 - 1

        # Numerator fits in 128 bits, so big int is not necessary
        # We multiply by the denominators here, so we only have one fraction in the end (avoiding floating point)
        term1 = (
            self.constants["DIFFICULTY_DELAY"]
            * Tp
            * (timestamp3 - timestamp2)
            * self.constants["BLOCK_TIME_TARGET"]
        )
        term2 = (
            (self.constants["DIFFICULTY_WARP_FACTOR"] - 1)
            * (self.constants["DIFFICULTY_EPOCH"] - self.constants["DIFFICULTY_DELAY"])
            * Tc
            * (timestamp2 - timestamp1)
            * self.constants["BLOCK_TIME_TARGET"]
        )

        # Round down after the division
        new_difficulty: uint64 = uint64(
            (term1 + term2)
            // (
                self.constants["DIFFICULTY_WARP_FACTOR"]
                * (timestamp3 - timestamp2)
                * (timestamp2 - timestamp1)
            )
        )

        # Only change by a max factor, to prevent attacks, as in greenpaper, and must be at least 1
        if new_difficulty >= Tc:
            return min(new_difficulty, uint64(self.constants["DIFFICULTY_FACTOR"] * Tc))
        else:
            return max(
                [
                    uint64(1),
                    new_difficulty,
                    uint64(Tc // self.constants["DIFFICULTY_FACTOR"]),
                ]
            )

    def get_next_ips(self, header_block: HeaderBlock) -> uint64:
        """
        Returns the VDF speed in iterations per seconds, to be used for the next block. This depends on
        the number of iterations of the last epoch, and changes at the same block as the difficulty.
        """
        block: SmallHeaderBlock = self.headers[header_block.header_hash]
        assert block.challenge is not None

        next_height: uint32 = uint32(block.height + 1)
        if next_height < self.constants["DIFFICULTY_EPOCH"]:
            # First epoch has a hardcoded vdf speed
            return self.constants["VDF_IPS_STARTING"]

        prev_block: SmallHeaderBlock = self.headers[block.prev_header_hash]
        assert prev_block.challenge is not None

        proof_of_space = header_block.proof_of_space
        difficulty = self.get_next_difficulty(prev_block.header_hash)
        iterations = uint64(
            block.challenge.total_iters - prev_block.challenge.total_iters
        )
        prev_ips = calculate_ips_from_iterations(
            proof_of_space, difficulty, iterations, self.constants["MIN_BLOCK_TIME"]
        )

        if (
            next_height % self.constants["DIFFICULTY_EPOCH"]
            != self.constants["DIFFICULTY_DELAY"]
        ):
            # Not at a point where ips would change, so return the previous ips
            # TODO: cache this for efficiency
            return prev_ips

        # ips (along with difficulty) will change in this block, so we need to calculate the new one.
        # The calculation is (iters_2 - iters_1) // (timestamp_2 - timestamp_1).
        # 1 and 2 correspond to height_1 and height_2, being the last block of the second to last, and last
        # block of the last epochs. Basically, it's total iterations over time, of previous epoch.

        # Height1 is the last block 2 epochs ago, so we can include the iterations taken for mining first block in epoch
        height1 = uint32(
            next_height
            - self.constants["DIFFICULTY_EPOCH"]
            - self.constants["DIFFICULTY_DELAY"]
            - 1
        )
        # Height2 is the last block in the previous epoch
        height2 = uint32(next_height - self.constants["DIFFICULTY_DELAY"] - 1)

        block1: Optional[SmallHeaderBlock] = None
        block2: Optional[SmallHeaderBlock] = None
        if block not in self.get_current_tips() or height2 not in self.height_to_hash:
            # This means we are either on a fork, or on one of the chains, but after the LCA,
            # so we manually backtrack.
            curr: Optional[SmallHeaderBlock] = block
            assert curr is not None
            while (
                curr.height not in self.height_to_hash
                or self.height_to_hash[curr.height] != curr.header_hash
            ):
                if curr.height == height1:
                    block1 = curr
                elif curr.height == height2:
                    block2 = curr
                curr = self.headers.get(curr.prev_header_hash, None)
                assert curr is not None
        # Once we are before the fork point (and before the LCA), we can use the height_to_hash map
        if block1 is None and height1 >= 0:
            # height1 could be -1, for the first difficulty calculation
            block1 = self.headers.get(self.height_to_hash[height1], None)
        if block2 is None:
            block2 = self.headers.get(self.height_to_hash[height2], None)
        assert block2 is not None
        assert block2.challenge is not None

        if block1 is not None:
            assert block1.challenge is not None
            timestamp1 = block1.header.data.timestamp
            iters1 = block1.challenge.total_iters
        else:
            # In the case of height == -1, there is no timestamp here, so assume the genesis block
            # took constants["BLOCK_TIME_TARGET"] seconds to mine.
            genesis: SmallHeaderBlock = self.headers[self.height_to_hash[uint32(0)]]
            timestamp1 = (
                genesis.header.data.timestamp - self.constants["BLOCK_TIME_TARGET"]
            )
            assert genesis.challenge is not None
            iters1 = genesis.challenge.total_iters

        timestamp2 = block2.header.data.timestamp
        iters2 = block2.challenge.total_iters

        new_ips = uint64((iters2 - iters1) // (timestamp2 - timestamp1))

        # Only change by a max factor, and must be at least 1
        if new_ips >= prev_ips:
            return min(new_ips, uint64(self.constants["IPS_FACTOR"] * new_ips))
        else:
            return max(
                [uint64(1), new_ips, uint64(prev_ips // self.constants["IPS_FACTOR"])]
            )

    async def receive_block(
        self,
        block: FullBlock,
        prev_block: Optional[HeaderBlock] = None,
        pre_validated: bool = False,
        pos_quality: bytes32 = None,
    ) -> Tuple[ReceiveBlockResult, Optional[SmallHeaderBlock]]:
        """
        Adds a new block into the blockchain, if it's valid and connected to the current
        blockchain, regardless of whether it is the child of a head, or another block.
        """
        genesis: bool = block.height == 0 and not self.tips

        if block.header_hash in self.headers:
            return ReceiveBlockResult.ALREADY_HAVE_BLOCK, None

        if block.prev_header_hash not in self.headers and not genesis:
            return ReceiveBlockResult.DISCONNECTED_BLOCK, None

        if not await self.validate_block(
            block, prev_block, genesis, pre_validated, pos_quality
        ):
            return ReceiveBlockResult.INVALID_BLOCK, None

        # Cache header in memory
        assert block.header_block.challenge is not None
        self.headers[block.header_hash] = block.header_block.to_small()

        # Always immediately add the block to the database, after updating blockchain state
        await self.store.add_block(block)
        res, header = await self._reconsider_heads(
            block.header_block.to_small(), genesis
        )
        if res:
            return ReceiveBlockResult.ADDED_TO_HEAD, header
        else:
            return ReceiveBlockResult.ADDED_AS_ORPHAN, None

    async def validate_unfinished_block(
        self,
        block: FullBlock,
        genesis: bool = False,
        pre_validated: bool = True,
        pos_quality: bytes32 = None,
    ) -> bool:
        """
        Block validation algorithm. Returns true if the candidate block is fully valid
        (except for proof of time). The same as validate_block, but without proof of time
        and challenge validation.
        """
        if not pre_validated:
            # 1. Check the proof of space hash is valid
            if (
                block.header_block.proof_of_space.get_hash()
                != block.header_block.header.data.proof_of_space_hash
            ):
                return False

            # 2. Check body hash
            if block.body.get_hash() != block.header_block.header.data.body_hash:
                return False

            # 3. Check coinbase signature with pool pk
            pair = block.body.coinbase_signature.AGGSIGPair(
                block.header_block.proof_of_space.pool_pubkey,
                block.body.coinbase.name(),
            )

            if not block.body.coinbase_signature.validate([pair]):
                return False

            # 4. Check harvester signature of header data is valid based on harvester key
            if not block.header_block.header.harvester_signature.verify(
                [blspy.Util.hash256(block.header_block.header.data.get_hash())],
                [block.header_block.proof_of_space.plot_pubkey],
            ):
                return False

        # 5. Check previous pointer(s) / flyclient
        if not genesis and block.prev_header_hash not in self.headers:
            return False

        # 6. Check Now+2hrs > timestamp > avg timestamp of last 11 blocks
        prev_block: Optional[SmallHeaderBlock] = None
        if not genesis:
            # TODO: do something about first 11 blocks
            last_timestamps: List[uint64] = []
            prev_block = self.headers.get(block.prev_header_hash, None)
            if not prev_block:
                return False
            curr = prev_block
            while len(last_timestamps) < self.constants["NUMBER_OF_TIMESTAMPS"]:
                last_timestamps.append(curr.header.data.timestamp)
                fetched = self.headers.get(curr.prev_header_hash, None)
                if not fetched:
                    break
                curr = fetched
            if (
                len(last_timestamps) != self.constants["NUMBER_OF_TIMESTAMPS"]
                and curr.height != 0
            ):
                return False
            prev_time: uint64 = uint64(int(sum(last_timestamps) / len(last_timestamps)))
            if block.header_block.header.data.timestamp < prev_time:
                return False
            if (
                block.header_block.header.data.timestamp
                > time.time() + self.constants["MAX_FUTURE_TIME"]
            ):
                return False

        # 7. Check filter hash is correct TODO

        # 8. Check extension data, if any is added

        # 9. Compute challenge of parent
        challenge_hash: bytes32
        if not genesis:
            assert prev_block
            assert prev_block.challenge
            challenge_hash = prev_block.challenge.get_hash()

            # 8. Check challenge hash of prev is the same as in pos
            if challenge_hash != block.header_block.proof_of_space.challenge_hash:
                return False
        else:
            assert block.header_block.proof_of_time
            challenge_hash = block.header_block.proof_of_time.challenge_hash

            if challenge_hash != block.header_block.proof_of_space.challenge_hash:
                return False

        # 10. Check proof of space based on challenge
        if pos_quality is None:
            pos_quality = block.header_block.proof_of_space.verify_and_get_quality()
            if not pos_quality:
                return False

        # 11. Check block height = prev height + 1
        if not genesis:
            assert prev_block
            if block.height != prev_block.height + 1:
                return False
        else:
            if block.height != 0:
                return False

        return True

    async def validate_block(
        self,
        block: FullBlock,
        prev_full_block: Optional[HeaderBlock] = None,
        genesis: bool = False,
        pre_validated: bool = False,
        pos_quality: bytes32 = None,
    ) -> bool:
        """
        Block validation algorithm. Returns true iff the candidate block is fully valid,
        and extends something in the blockchain.
        """
        # 1. Validate unfinished block (check the rest of the conditions)
        if not (
            await self.validate_unfinished_block(
                block, genesis, pre_validated, pos_quality
            )
        ):
            return False

        difficulty: uint64
        ips: uint64
        if not genesis:
            difficulty = self.get_next_difficulty(block.prev_header_hash)
            assert prev_full_block is not None
            ips = self.get_next_ips(prev_full_block)
        else:
            difficulty = uint64(self.constants["DIFFICULTY_STARTING"])
            ips = uint64(self.constants["VDF_IPS_STARTING"])

        # 2. Check proof of space hash
        if not pre_validated:
            if not block.header_block.challenge or not block.header_block.proof_of_time:
                return False
            if (
                block.header_block.proof_of_space.get_hash()
                != block.header_block.challenge.proof_of_space_hash
            ):
                return False

        # 3. Check number of iterations on PoT is correct, based on prev block and PoS
        if pos_quality is None:
            pos_quality = block.header_block.proof_of_space.verify_and_get_quality()

        if pos_quality is None:
            return False

        number_of_iters: uint64 = calculate_iterations_quality(
            pos_quality,
            block.header_block.proof_of_space.size,
            difficulty,
            ips,
            self.constants["MIN_BLOCK_TIME"],
        )

        if block.header_block.proof_of_time is None:
            return False

        if number_of_iters != block.header_block.proof_of_time.number_of_iterations:
            return False

        # 4. Check PoT
        if not pre_validated:
            if not block.header_block.proof_of_time.is_valid(
                self.constants["DISCRIMINANT_SIZE_BITS"]
            ):
                return False

        if block.header_block.challenge is None:
            return False

        if not genesis:
            prev_block: Optional[SmallHeaderBlock] = self.headers.get(
                block.prev_header_hash, None
            )
            if not prev_block or not prev_block.challenge:
                return False

            # 5. and check if PoT.challenge_hash matches
            if (
                block.header_block.proof_of_time.challenge_hash
                != prev_block.challenge.get_hash()
            ):
                return False

            # 6a. Check challenge total_weight = parent total_weight + difficulty
            if (
                block.header_block.challenge.total_weight
                != prev_block.challenge.total_weight + difficulty
            ):
                return False

            # 7a. Check challenge total_iters = parent total_iters + number_iters
            if (
                block.header_block.challenge.total_iters
                != prev_block.challenge.total_iters + number_of_iters
            ):
                return False

            coinbase_reward = calculate_block_reward(block.height)
            if (coinbase_reward / 8) * 7 != block.body.coinbase.amount:
                return False
            fee_base = uint64(int(coinbase_reward / 8))
            # 8. If there is no agg signature, there should be no transactions either
            # target reward_fee = 1/8 coinbase reward + tx fees
            if not block.body.aggregated_signature:
                if block.body.transactions:
                    return False
                else:
                    if fee_base != block.body.fees_coin.amount:
                        return False
            else:
                # Validate transactions, and verify that fee_base + TX fees = fee_coin.amount
                err = await self.validate_transactions(block, fee_base)
                if err:
                    return False
        else:
            # 6b. Check challenge total_weight = parent total_weight + difficulty
            if block.header_block.challenge.total_weight != difficulty:
                return False

            # 7b. Check challenge total_iters = parent total_iters + number_iters
            if block.header_block.challenge.total_iters != number_of_iters:
                return False

        return True

    async def pre_validate_blocks(
        self, blocks: List[FullBlock]
    ) -> List[Tuple[bool, Optional[bytes32]]]:

        results = []
        for block in blocks:
            val, pos = self.pre_validate_block_multi(bytes(block))
            if pos is not None:
                pos = bytes32(pos)
            results.append((val, pos))

        return results

    async def pre_validate_blocks_multiprocessing(
        self, blocks: List[FullBlock]
    ) -> List[Tuple[bool, Optional[bytes32]]]:
        futures = []

        cpu_count = multiprocessing.cpu_count()
        # Pool of workers to validate blocks concurrently
        pool = concurrent.futures.ProcessPoolExecutor(max_workers=max(cpu_count - 1, 1))

        for block in blocks:
            futures.append(
                asyncio.get_running_loop().run_in_executor(
                    pool, self.pre_validate_block_multi, bytes(block)
                )
            )
        results = await asyncio.gather(*futures)

        for i, (val, pos) in enumerate(results):
            if pos is not None:
                pos = bytes32(pos)
            results[i] = val, pos
        pool.shutdown(wait=True)
        return results

    @staticmethod
    def pre_validate_block_multi(data) -> Tuple[bool, Optional[bytes]]:
        """
            Validates all parts of FullBlock that don't need to be serially checked
        """
        block = FullBlock.from_bytes(data)

        if not block.header_block.challenge or not block.header_block.proof_of_time:
            return False, None
        if (
            block.header_block.proof_of_space.get_hash()
            != block.header_block.challenge.proof_of_space_hash
        ):
            return False, None
            # 4. Check PoT
        if not block.header_block.proof_of_time.is_valid(
            consensus_constants["DISCRIMINANT_SIZE_BITS"]
        ):
            return False, None

        # 9. Check harvester signature of header data is valid based on harvester key
        if not block.header_block.header.harvester_signature.verify(
            [blspy.Util.hash256(block.header_block.header.data.get_hash())],
            [block.header_block.proof_of_space.plot_pubkey],
        ):
            return False, None

        # 10. Check proof of space based on challenge
        pos_quality = block.header_block.proof_of_space.verify_and_get_quality()

        if not pos_quality:
            return False, None

        return True, bytes(pos_quality)

    def _reconsider_heights(
        self, old_lca: Optional[SmallHeaderBlock], new_lca: SmallHeaderBlock
    ):
        """
        Update the mapping from height to block hash, when the lca changes.
        """
        curr_old: Optional[SmallHeaderBlock] = old_lca if old_lca else None
        curr_new: SmallHeaderBlock = new_lca
        while True:
            fetched: Optional[SmallHeaderBlock]
            if not curr_old or curr_old.height < curr_new.height:
                self.height_to_hash[uint32(curr_new.height)] = curr_new.header_hash
                self.headers[curr_new.header_hash] = curr_new
                if curr_new.height == 0:
                    return
                curr_new = self.headers[curr_new.prev_header_hash]
            elif curr_old.height > curr_new.height:
                del self.height_to_hash[uint32(curr_old.height)]
                curr_old = self.headers[curr_old.prev_header_hash]
            else:
                if curr_new.header_hash == curr_old.header_hash:
                    return
                self.height_to_hash[uint32(curr_new.height)] = curr_new.header_hash
                curr_new = self.headers[curr_new.prev_header_hash]
                curr_old = self.headers[curr_old.prev_header_hash]

    async def _reconsider_lca(self, genesis: bool):
        """
        Update the least common ancestor of the heads. This is useful, since we can just assume
        there is one block per height before the LCA (and use the height_to_hash dict).
        """
        cur: List[SmallHeaderBlock] = self.tips[:]
        lca_tmp: Optional[SmallHeaderBlock]
        try:
            lca_tmp = self.lca_block
        except AttributeError:
            lca_tmp = None
        while any(b.header_hash != cur[0].header_hash for b in cur):
            heights = [b.height for b in cur]
            i = heights.index(max(heights))
            cur[i] = self.headers[cur[i].prev_header_hash]
        if genesis:
            self._reconsider_heights(None, cur[0])
        else:
            self._reconsider_heights(self.lca_block, cur[0])
        self.lca_block = cur[0]

        if lca_tmp is None:
            full: Optional[FullBlock] = await self.store.get_block(
                self.lca_block.header_hash
            )
            if full is None:
                return
            await self.unspent_store.new_lca(full)
            await self.create_diffs_for_tips(self.lca_block)
        # If LCA changed update the unspent store
        elif lca_tmp.header_hash != self.lca_block.header_hash:
            if self.lca_block.height < lca_tmp.height:
                if self.is_descendant(lca_tmp, self.lca_block):
                    # new LCA is lower height than the new LCA (linear REORG)
                    await self.unspent_store.rollback_lca_to_block(
                        self.lca_block.height
                    )
                    # Nuke DiffStore
                    self.unspent_store.nuke_diffs()
                    # Create DiffStore
                    await self.create_diffs_for_tips(self.lca_block)
                else:
                    # New LCA is lower height but not the a parent of old LCA (Reorg)
                    fork_h = self.find_fork_for_lca(lca_tmp)
                    # Rollback to fork
                    await self.unspent_store.rollback_lca_to_block(fork_h)
                    # Nuke DiffStore
                    self.unspent_store.nuke_diffs()
                    # Add blocks between fork point and new lca
                    fork_hash = self.height_to_hash[fork_h]
                    fork_head = self.headers[fork_hash]
                    await self._from_fork_to_lca(fork_head, self.lca_block)
                    # Create DiffStore
                    await self.create_diffs_for_tips(self.lca_block)
            if self.lca_block.height >= lca_tmp.height:
                if self.lca_block.prev_header_hash == lca_tmp.header_hash:
                    # New LCA is a child of the old one, just add it
                    full = await self.store.get_block(self.lca_block.header_hash)
                    if full is None:
                        return
                    await self.unspent_store.new_lca(full)
                    # Nuke DiffStore
                    self.unspent_store.nuke_diffs()
                    # Create DiffStore
                    await self.create_diffs_for_tips(self.lca_block)
                else:
                    if self.is_descendant(self.lca_block, lca_tmp):
                        # Add blocks between old and new lca block
                        await self._from_fork_to_lca(lca_tmp, self.lca_block)
                        # Nuke DiffStore
                        self.unspent_store.nuke_diffs()
                        # Create DiffStore
                        await self.create_diffs_for_tips(self.lca_block)
                    else:
                        # Find Fork
                        fork_h = self.find_fork_for_lca(lca_tmp)
                        # Rollback to fork_point
                        await self.unspent_store.rollback_lca_to_block(fork_h)
                        # Add blocks from fork_point to new_lca
                        fork_hash = self.height_to_hash[fork_h]
                        fork_head = self.headers[fork_hash]
                        await self._from_fork_to_lca(fork_head, self.lca_block)
                        #  Nuke DiffStore
                        self.unspent_store.nuke_diffs()
                        # Create DiffStore
                        await self.create_diffs_for_tips(self.lca_block)
        else:
            # If LCA has not changes just update the difference
            self.unspent_store.nuke_diffs()
            # Create DiffStore
            await self.create_diffs_for_tips(self.lca_block)

    # TODO Ask Mariano about this
    def find_fork_for_lca(self, old_lca: SmallHeaderBlock) -> uint32:
        """ Tries to find height where new chain (current) diverged from the old chain where old_lca was the LCA"""
        tmp_old: SmallHeaderBlock = old_lca
        while tmp_old.header_hash != self.genesis.header_hash:
            if tmp_old.header_hash == self.genesis.header_hash:
                return uint32(0)
            if tmp_old.height in self.height_to_hash:
                chain_hash_at_h = self.height_to_hash[tmp_old.height]
                if chain_hash_at_h == tmp_old.header_hash:
                    return tmp_old.height
            tmp_old = self.headers[tmp_old.prev_header_hash]
        return uint32(0)

    def is_descendant(
        self, child: SmallHeaderBlock, maybe_parent: SmallHeaderBlock
    ) -> bool:
        """ Goes backward from potential child until it reaches potential parent or genesis"""
        current = child

        while current.header_hash != self.genesis.header_hash:
            if current.header_hash == maybe_parent.header_hash:
                return True
            current = self.headers[current.prev_header_hash]
            if maybe_parent.height < current.height:
                break

        return False

    async def create_diffs_for_tips(self, target: SmallHeaderBlock):
        """ Adds to unspent store from tips down to target"""
        for tip in self.tips:
            await self._from_tip_to_lca_unspent(tip, target)

    async def get_full_tips(self) -> List[FullBlock]:
        """ Return list of FullBlocks that are tips"""
        result: List[FullBlock] = []
        for tip in self.tips:
            block = await self.store.get_block(tip.header_hash)
            if not block:
                continue
            result.append(block)
        return result

    async def _from_tip_to_lca_unspent(
        self, head: SmallHeaderBlock, target: SmallHeaderBlock
    ):
        """ Adds diffs to unspent store, from tip to lca target"""
        blocks: List[FullBlock] = []
        tip_hash: bytes32 = head.header_hash
        while True:
            if tip_hash == target.header_hash:
                break
            full = await self.store.get_block(tip_hash)
            if full is None:
                return
            blocks.append(full)
            tip_hash = full.header_block.prev_header_hash
        if len(blocks) == 0:
            return
        blocks.reverse()
        await self.unspent_store.new_heads(blocks)

    async def _from_fork_to_lca(
        self, fork_point: SmallHeaderBlock, lca: SmallHeaderBlock
    ):
        """ Selects blocks between fork_point and LCA, and then adds them to unspent_store. """
        blocks: List[FullBlock] = []
        tip_hash: bytes32 = lca.header_hash
        while True:
            if tip_hash == fork_point.header_hash:
                break
            full = await self.store.get_block(tip_hash)
            if not full:
                return
            blocks.append(full)
            tip_hash = full.header_block.prev_header_hash
        blocks.reverse()

        await self.unspent_store.add_lcas(blocks)

    async def _reconsider_heads(
        self, block: SmallHeaderBlock, genesis: bool
    ) -> Tuple[bool, Optional[SmallHeaderBlock]]:
        """
        When a new block is added, this is called, to check if the new block is heavier
        than one of the heads.
        """
        removed: Optional[SmallHeaderBlock] = None
        if len(self.tips) == 0 or block.weight > min([b.weight for b in self.tips]):
            self.tips.append(block)
            while len(self.tips) > self.constants["NUMBER_OF_HEADS"]:
                self.tips.sort(key=lambda b: b.weight, reverse=True)
                # This will loop only once
                removed = self.tips.pop()
            await self._reconsider_lca(genesis)
            return True, removed
        return False, None

    async def validate_transactions(
        self, block: FullBlock, fee_base: uint64
    ) -> Optional[Err]:

        if not block.body.transactions:
            return Err.UNKNOWN
        # Get List of names removed, puzzles hashes for removed coins and conditions crated
        error, npc_list, cost = await get_name_puzzle_conditions(
            block.body.transactions
        )

        if cost > 6000:
            return Err.BLOCK_COST_EXCEEDS_MAX
        if error:
            return error

        prev_header: SmallHeaderBlock
        if block.prev_header_hash in self.headers:
            prev_header = self.headers[block.prev_header_hash]
        else:
            return Err.EXTENDS_UNKNOWN_BLOCK

        removals: List[bytes32] = []
        removals_puzzle_dic: Dict[bytes32, bytes32] = {}
        for npc in npc_list:
            removals.append(npc.coin_name)
            removals_puzzle_dic[npc.coin_name] = npc.puzzle_hash

        additions: List[Coin] = additions_for_npc(npc_list)
        additions_dic: Dict[bytes32, Coin] = {}
        # Check additions for max coin amount
        for coin in additions:
            additions_dic[coin.name()] = coin
            if coin.amount >= consensus_constants["MAX_COIN_AMOUNT"]:
                return Err.COIN_AMOUNT_EXCEEDS_MAXIMUM

        # Watch out for duplicate outputs
        addition_counter = collections.Counter(_.name() for _ in additions)
        for k, v in addition_counter.items():
            if v > 1:
                return Err.DUPLICATE_OUTPUT

        # Check for duplicate spends inside block
        removal_counter = collections.Counter(removals)
        for k, v in removal_counter.items():
            if v > 1:
                return Err.DOUBLE_SPEND

        # Check if removals exist and were not previously spend. (unspent_db + diff_store + this_block)
        removal_unspents: Dict[bytes32, Unspent] = {}
        for rem in removals:
            if rem in additions_dic:
                # Ephemeral coin
                rem_coin: Coin = additions_dic[rem]
                new_unspent: Unspent = Unspent(rem_coin, block.height, 0, 0, 0)  # type: ignore # noqa
                removal_unspents[new_unspent.name] = new_unspent
            else:
                assert prev_header is not None
                unspent = await self.unspent_store.get_unspent(rem, prev_header)
                if unspent:
                    if unspent.spent == 1:
                        return Err.DOUBLE_SPEND
                    if unspent.coinbase == 1:
                        if (
                            block.height
                            < unspent.confirmed_block_index + self.coinbase_freeze
                        ):
                            return Err.COINBASE_NOT_YET_SPENDABLE
                    removal_unspents[unspent.name] = unspent
                else:
                    return Err.UNKNOWN_UNSPENT

        # Check fees
        removed = 0
        for unspent in removal_unspents.values():
            removed += unspent.coin.amount

        added = 0
        for coin in additions:
            added += coin.amount

        if removed < added:
            return Err.MINTING_COIN

        fees = removed - added

        # Check coinbase reward
        if fees + fee_base != block.body.fees_coin.amount:
            return Err.BAD_COINBASE_REWARD

        # Verify that removed coin puzzle_hashes match with calculated puzzle_hashes
        for unspent in removal_unspents.values():
            if unspent.coin.puzzle_hash != removals_puzzle_dic[unspent.name]:
                return Err.WRONG_PUZZLE_HASH

        # Verify conditions, create hash_key list for aggsig check
        hash_key_pairs = []
        for npc in npc_list:
            unspent = removal_unspents[npc.coin_name]
            error = blockchain_check_conditions_dict(
                unspent,
                removal_unspents,
                npc.condition_dict,
                block.header_block.to_small(),
            )
            if error:
                return error
            hash_key_pairs.extend(
                hash_key_pairs_for_conditions_dict(npc.condition_dict)
            )

        # Verify aggregated signature
        if not block.body.aggregated_signature:
            return Err.BAD_AGGREGATE_SIGNATURE
        if not block.body.aggregated_signature.validate(hash_key_pairs):
            return Err.BAD_AGGREGATE_SIGNATURE

        return None
