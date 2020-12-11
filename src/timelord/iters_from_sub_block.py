from typing import Union, Tuple, Optional

from src.consensus.pot_iterations import (
    calculate_iterations_quality,
    calculate_sp_iters,
    calculate_ip_iters,
)
from src.types.reward_chain_sub_block import (
    RewardChainSubBlock,
    RewardChainSubBlockUnfinished,
)
from src.types.sized_bytes import bytes32
from src.util.ints import uint64


def iters_from_sub_block(
    constants,
    reward_chain_sub_block: Union[RewardChainSubBlock, RewardChainSubBlockUnfinished],
    sub_slot_iters: uint64,
    difficulty: uint64,
) -> Tuple[uint64, uint64]:
    if reward_chain_sub_block.challenge_chain_sp_vdf is None:
        assert reward_chain_sub_block.signage_point_index == 0
        cc_sp: bytes32 = reward_chain_sub_block.pos_ss_cc_challenge_hash
    else:
        cc_sp = reward_chain_sub_block.challenge_chain_sp_vdf.output.get_hash()

    quality_string: Optional[bytes32] = reward_chain_sub_block.proof_of_space.verify_and_get_quality_string(
        constants,
        reward_chain_sub_block.pos_ss_cc_challenge_hash,
        cc_sp,
    )
    assert quality_string is not None

    required_iters: uint64 = calculate_iterations_quality(
        quality_string,
        reward_chain_sub_block.proof_of_space.size,
        difficulty,
        cc_sp,
    )
    return (
        calculate_sp_iters(constants, sub_slot_iters, reward_chain_sub_block.signage_point_index),
        calculate_ip_iters(
            constants,
            sub_slot_iters,
            reward_chain_sub_block.signage_point_index,
            required_iters,
        ),
    )
