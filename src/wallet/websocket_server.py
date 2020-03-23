import asyncio
import dataclasses
import json
import traceback

from typing import Any, Dict, List
from src.server.outbound_message import NodeType, OutboundMessage, Message, Delivery
from src.simulator.simulator_protocol import FarmNewBlockProtocol
from src.wallet.rl_wallet.rl_wallet import RLWallet
from src.wallet.util.wallet_types import WalletType
from src.wallet.wallet_info import WalletInfo
from src.wallet.wallet_node import WalletNode


class EnhancedJSONEncoder(json.JSONEncoder):
    """
    Encodes bytes as hex strings with 0x, and converts all dataclasses to json.
    """

    def default(self, o: Any):
        if dataclasses.is_dataclass(o):
            return o.to_json()
        elif isinstance(o, WalletType):
            return o.name
        elif hasattr(type(o), "__bytes__"):
            return f"0x{bytes(o).hex()}"
        return super().default(o)


def obj_to_response(o: Any) -> str:
    """
    Converts a python object into json.
    """
    json_str = json.dumps(o, cls=EnhancedJSONEncoder, sort_keys=True)
    return json_str


def format_response(command: str, response_data: Dict[str, Any]):
    """
    Formats the response into standard format used between renderer.js and here
    """
    response = {"command": command, "data": response_data}

    json_str = obj_to_response(response)
    return json_str


class WebSocketServer:
    def __init__(self, wallet_node: WalletNode, log):
        self.wallet_node: WalletNode = wallet_node
        self.websocket = None
        self.log = log

    async def get_next_puzzle_hash(self, websocket, request, response_api):
        """
        Returns a new puzzlehash
        """

        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallets[wallet_id]
        puzzlehash = (await wallet.get_new_puzzlehash()).hex()

        data = {
            "puzzlehash": puzzlehash,
        }

        await websocket.send(format_response(response_api, data))

    async def send_transaction(self, websocket, request, response_api):

        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallets[wallet_id]

        tx = await wallet.generate_signed_transaction_dict(request)

        if tx is None:
            data = {"success": False}
            return await websocket.send(format_response(response_api, data))

        await wallet.push_transaction(tx)

        data = {"success": True}
        return await websocket.send(format_response(response_api, data))

    async def server_ready(self, websocket, response_api):
        response = {"success": True}
        await websocket.send(format_response(response_api, response))

    async def get_transactions(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        transactions = await self.wallet_node.wallet_state_manager.get_all_transactions(
            wallet_id
        )

        response = {"success": True, "txs": transactions}
        await websocket.send(format_response(response_api, response))

    async def farm_block(self, websocket, request, response_api):
        puzzle_hash = bytes.fromhex(request["puzzle_hash"])
        request = FarmNewBlockProtocol(puzzle_hash)
        msg = OutboundMessage(
            NodeType.FULL_NODE, Message("farm_new_block", request), Delivery.BROADCAST,
        )

        self.wallet_node.server.push_message(msg)

    async def get_wallet_balance(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallets[wallet_id]
        balance = await wallet.get_confirmed_balance()
        pending_balance = await wallet.get_unconfirmed_balance()

        response = {
            "wallet_id": wallet_id,
            "success": True,
            "confirmed_wallet_balance": balance,
            "unconfirmed_wallet_balance": pending_balance,
        }

        await websocket.send(format_response(response_api, response))

    async def get_sync_status(self, websocket, response_api):
        syncing = self.wallet_node.wallet_state_manager.sync_mode

        response = {"syncing": syncing}

        await websocket.send(format_response(response_api, response))

    async def get_height_info(self, websocket, response_api):
        lca = self.wallet_node.wallet_state_manager.lca
        height = self.wallet_node.wallet_state_manager.block_records[lca].height

        response = {"height": height}

        await websocket.send(format_response(response_api, response))

    async def get_connection_info(self, websocket, response_api):
        connections = (
            self.wallet_node.server.global_connections.get_full_node_peerinfos()
        )

        response = {"connections": connections}

        await websocket.send(format_response(response_api, response))

    async def create_new_wallet(self, websocket, request, response_api):
        config, key_config, wallet_state_manager, main_wallet = self.get_wallet_config()
        if request["wallet_type"] == "rl_wallet":
            if request["mode"] == "admin":
                rl_admin: RLWallet = await RLWallet.create_rl_admin(
                    config, key_config, wallet_state_manager, main_wallet
                )
                self.wallet_node.wallets[rl_admin.wallet_info.id] = rl_admin
                response = {"success": True, "type": "rl_wallet"}
                return await websocket.send(format_response(response_api, response))
            elif request["mode"] == "user":
                rl_user: RLWallet = await RLWallet.create_rl_user(
                    config, key_config, wallet_state_manager, main_wallet
                )
                self.wallet_node.wallets[rl_user.wallet_info.id] = rl_user
                response = {"success": True, "type": "rl_wallet"}
                return await websocket.send(format_response(response_api, response))
        elif request["wallet_type"] == "cc_wallet":
            print("Create me!!")

        response = {"success": False}
        return await websocket.send(format_response(response_api, response))

    def get_wallet_config(self):
        return (
            self.wallet_node.config,
            self.wallet_node.key_config,
            self.wallet_node.wallet_state_manager,
            self.wallet_node.main_wallet,
        )

    async def get_wallets(self, websocket, response_api):
        wallets: List[
            WalletInfo
        ] = await self.wallet_node.wallet_state_manager.get_all_wallets()

        response = {"wallets": wallets}

        return await websocket.send(format_response(response_api, response))

    async def safe_handle(self, websocket, path):
        try:
            await self.handle_message(websocket, path)
        except BaseException:
            tb = traceback.format_exc()
            self.log.error(f"Error while handling message: {tb}")

    async def handle_message(self, websocket, path):
        """
        This function gets called when new message is received via websocket.
        """

        async for message in websocket:
            decoded = json.loads(message)
            command = decoded["command"]
            data = None
            if "data" in decoded:
                data = decoded["data"]
            if command == "start_server":
                self.websocket = websocket
                await self.server_ready(websocket, command)
            elif command == "get_wallet_balance":
                await self.get_wallet_balance(websocket, data, command)
            elif command == "send_transaction":
                await self.send_transaction(websocket, data, command)
            elif command == "get_next_puzzle_hash":
                await self.get_next_puzzle_hash(websocket, data, command)
            elif command == "get_transactions":
                await self.get_transactions(websocket, data, command)
            elif command == "farm_block":
                await self.farm_block(websocket, data, command)
            elif command == "get_sync_status":
                await self.get_sync_status(websocket, command)
            elif command == "get_height_info":
                await self.get_height_info(websocket, command)
            elif command == "get_connection_info":
                await self.get_connection_info(websocket, command)
            elif command == "create_new_wallet":
                await self.create_new_wallet(websocket, data, command)
            elif command == "get_wallets":
                await self.get_wallets(websocket, command)
            else:
                response = {"error": f"unknown_command {command}"}
                await websocket.send(obj_to_response(response))

    async def notify_ui_that_state_changed(self, state: str):
        data = {
            "state": state,
        }
        if self.websocket is not None:
            await self.websocket.send(format_response("state_changed", data))

    def state_changed_callback(self, state: str):
        if self.websocket is None:
            return
        asyncio.ensure_future(self.notify_ui_that_state_changed(state))
