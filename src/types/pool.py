from typing import List, Dict

from sortedcontainers import SortedDict

from src.types.hashable import Coin, CoinName
from src.types.header_block import HeaderBlock
from src.types.mempool_item import MempoolItem
from src.types.sized_bytes import bytes32
from src.util.ints import uint32, uint64


class Pool:
    header_block: HeaderBlock
    spends: Dict[bytes32, MempoolItem]
    sorted_spends: SortedDict
    additions: Dict[CoinName, MempoolItem]
    removals: Dict[CoinName, MempoolItem]
    min_fee: uint64
    size: uint32

    # if new min fee is added
    @staticmethod
    async def create(head: HeaderBlock, size: uint32):
        self = Pool()
        self.header_block = head
        self.spends = {}
        self.additions = {}
        self.removals = {}
        self.min_fee = 0
        self.sorted_spends = SortedDict()
        self.size = size
        return self

    def get_min_fee_rate(self) -> float:
        if self.at_full_capacity():
            fee_per_cost, val = self.sorted_spends.peekitem(index=0)
            return fee_per_cost
        else:
            return 0

    def remove_spend(self, item: MempoolItem):
        removals: List[Coin] = item.spend_bundle.removals()
        additions: List[Coin] = item.spend_bundle.additions()
        for rem in removals:
            del self.removals[rem.name()]
        for add in additions:
            del self.additions[add.name()]
        del self.spends[item.name]
        del self.sorted_spends[item.fee_per_cost][item.name]
        dic = self.sorted_spends[item.fee_per_cost]
        if len(dic.values) == 0:
            del self.sorted_spends[item.fee_per_cost]

    def add_to_pool(self, item: MempoolItem, additions: List[Coin], removals_dic: Dict[bytes32, Coin]):
        if self.at_full_capacity():
            # Val is Dict[hash, MempoolItem]
            fee_per_cost, val = self.sorted_spends.peekitem(index=0)
            to_remove = val.values()[0]
            self.remove_spend(to_remove)

        self.spends[item.name] = item
        self.sorted_spends[item.fee_per_cost] = item

        for add in additions:
            self.additions[add.name()] = item
        for key in removals_dic.keys():
            self.removals[key] = item

    def at_full_capacity(self) -> bool:
        return len(self.spends.keys()) >= self.size
