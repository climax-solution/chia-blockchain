import asyncio
from typing import Any, Dict

import pytest
from src.consensus.constants import constants
from src.types.full_block import FullBlock
from src.unspent_store import UnspentStore
from tests.block_tools import BlockTools

bt = BlockTools()

test_constants: Dict[str, Any] = {
    "DIFFICULTY_STARTING": 5,
    "DISCRIMINANT_SIZE_BITS": 16,
    "BLOCK_TIME_TARGET": 10,
    "MIN_BLOCK_TIME": 2,
    "DIFFICULTY_FACTOR": 3,
    "DIFFICULTY_EPOCH": 12,  # The number of blocks per epoch
    "DIFFICULTY_WARP_FACTOR": 4,  # DELAY divides EPOCH in order to warp efficiently.
    "DIFFICULTY_DELAY": 3,  # EPOCH / WARP_FACTOR
}
test_constants["GENESIS_BLOCK"] = bytes(
    bt.create_genesis_block(test_constants, bytes([0] * 32), b"0")
)


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop


class TestUnspent:
    @pytest.mark.asyncio
    async def test_basic_unspent_store(self):
        blocks = bt.get_consecutive_blocks(test_constants, 9, [], 9, b"0")

        db = await UnspentStore.create("fndb_test")
        await db._clear_database()

        genesis = FullBlock.from_bytes(constants["GENESIS_BLOCK"])

        # Save/get block
        for block in blocks:
            await db.new_lca(block)
            unspent = await db.get_unspent(block.body.coinbase.name())
            unspent_fee = await db.get_unspent(block.body.fees_coin.name())
            assert block.body.coinbase == unspent.coin
            assert block.body.fees_coin == unspent_fee.coin
