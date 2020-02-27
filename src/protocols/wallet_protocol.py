from dataclasses import dataclass
from typing import List, Tuple

from src.types.body import Body
from src.types.hashable.coin import Coin
from src.types.hashable.spend_bundle import SpendBundle
from src.types.header_block import HeaderBlock
from src.types.sized_bytes import bytes32
from src.util.cbor_message import cbor_message
from src.util.ints import uint32


"""
Protocol between wallet (SPV node) and full node.
"""


@dataclass(frozen=True)
@cbor_message
class SendTransaction:
    transaction: SpendBundle


@dataclass(frozen=True)
@cbor_message
class TransactionAck:
    txid: bytes32
    status: bool


@dataclass(frozen=True)
@cbor_message
class NewLCA:
    lca_hash: bytes32
    height: uint32
    weight: uint32


@dataclass(frozen=True)
@cbor_message
class RequestHeader:
    header_hash: bytes32


@dataclass(frozen=True)
@cbor_message
class Header:
    header_block: HeaderBlock
    bip158_filter: bytes


@dataclass(frozen=True)
@cbor_message
class RequestAncestors:
    header_hash: bytes32
    previous_heights_desired: List[uint32]


@dataclass(frozen=True)
@cbor_message
class Ancestors:
    header_hash: bytes32
    List[Tuple[uint32, bytes32]]


@dataclass(frozen=True)
@cbor_message
class RequestBody:
    body_hash: bytes32


@dataclass(frozen=True)
@cbor_message
class RespondBody:
    body: Body
    height: uint32


@dataclass(frozen=True)
@cbor_message
class FullProofForHash:
    proof_hash: bytes32
    proof: bytes32


@dataclass(frozen=True)
@cbor_message
class ProofHash:
    proof_hash: bytes32


@dataclass(frozen=True)
@cbor_message
class RequestAdditions:
    height: uint32
    header_hash: bytes32


@dataclass(frozen=True)
@cbor_message
class RequestRemovals:
    height: uint32
    header_hash: bytes32


@dataclass(frozen=True)
@cbor_message
class Additions:
    height: uint32
    header_hash: bytes32
    coins: List[Coin]


@dataclass(frozen=True)
@cbor_message
class Removals:
    height: uint32
    header_hash: bytes32
    coins: List[Coin]
