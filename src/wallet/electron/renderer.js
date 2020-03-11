const host = "ws://127.0.0.1:9256"
const jquery = require('jquery')
var QRCode = require('qrcode')
var canvas = document.getElementById('qr_canvas')
const Dialogs = require('dialogs')
const dialogs = Dialogs()
const WebSocket = require('ws');
var ws = new WebSocket(host);

let send = document.querySelector('#send')
let farm_button = document.querySelector('#farm_block')
let new_address = document.querySelector('#new_address')
let copy = document.querySelector("#copy")
let receiver_address = document.querySelector("#receiver_puzzle_hash")
let amount = document.querySelector("#amount_to_send")
let table = document.querySelector("#tx_table").getElementsByTagName('tbody')[0]
let green_checkmark = "<i class=\"icon ion-md-checkmark-circle-outline green\"></i>"
let red_checkmark = "<i class=\"icon ion-md-close-circle-outline red\"></i>"
var myBalance = 0
var myUnconfirmedBalance = 0

function sleep(ms) {
    return new Promise((resolve) => {
        setTimeout(resolve, ms);
    });
}

function set_callbacks(socket) {
    /*
    Sets callbacks for socket events
    */

    socket.on('open', function open() {
        var msg = {"command": "start_server"}
        ws.send(JSON.stringify(msg));
    });

    socket.on('message', function incoming(incoming) {
        var message = JSON.parse(incoming);
        var command = message["command"];
        var data = message["data"];

        console.log("Received command: " + command);
        if (data) {
            console.log("Received message data: " + JSON.stringify(data));
        }

        if (command == "start_server") {
            get_new_puzzlehash();
            get_transactions();
            get_wallet_balance();
        } else if (command == "get_next_puzzle_hash") {
            get_new_puzzlehash_response(data);
        } else if (command == "get_wallet_balance") {
            get_wallet_balance_response(data);
        } else if (command == "send_transaction") {
            send_transaction_response(data);
        } else if (command == "get_transactions") {
            get_transactions_response(data);
        }
    });

    socket.on('error', function clear() {
        console.log("Not connected, reconnecting");
        connect(100);
    });
}

set_callbacks(ws);

async function connect(timeout) {
    /*
    Tries to connect to the host after a timeout
    */
    await sleep(timeout);
    ws = new WebSocket(host);
    set_callbacks(ws);
}

send.addEventListener('click', () => {
    /*
    Called when send button in ui is pressed.
    */
    puzzlehash = receiver_address.value;
    amount_value = amount.value;
    data = {
        "puzzlehash": puzzlehash,
        "amount": amount_value
    }

    request = {
        "command": "send_transaction",
        "data": data
    }
    json_data = JSON.stringify(request);
    ws.send(json_data);
})

farm_button.addEventListener('click', () => {
    /*
    Called when send button in ui is pressed.
    */
    console.log("farm block")
    puzzle_hash = receiver_address.value;
    if (puzzle_hash == "") {
        dialogs.alert("Specify puzzle_hash for coinbase reward", ok => {
        })
        return
    }
    data = {
        "puzzle_hash": puzzle_hash,
    }
    request = {
        "command": "farm_block",
        "data": data
    }
    json_data = JSON.stringify(request);
    ws.send(json_data);
})

function send_transaction_response(response) {
    /*
    Called when response is received for send_transaction request
    */
    success = response["success"];
    if (!success) {
        dialogs.alert("You don\'t have enough chia for this transactions", ok => {
        })
        return
    }
}

new_address.addEventListener('click', () => {
    /*
    Called when new address button is pressed.
    */
    console.log("new address requesting");
    get_new_puzzlehash(0);
})

copy.addEventListener("click", () => {
    /*
    Called when copy button is pressed
    */
    let puzzle_holder = document.querySelector("#puzzle_holder");
    puzzle_holder.select();
    /* Copy the text inside the text field */
    document.execCommand("copy");
})

async function get_new_puzzlehash() {
    /*
    Sends websocket request for new puzzle_hash
    */
    data = {
        "command": "get_next_puzzle_hash",
    }
    json_data = JSON.stringify(data);
    ws.send(json_data);
}

function get_new_puzzlehash_response(response) {
    /*
    Called when response is received for get_new_puzzle_hash request
    */
    let puzzle_holder = document.querySelector("#puzzle_holder");
    puzzle_holder.value = response["puzzlehash"];
    QRCode.toCanvas(canvas, response["puzzlehash"], function (error) {
    if (error) console.error(error)
    console.log('success!');
    })
}

async function get_wallet_balance() {
    /*
    Sends websocket request to get wallet balance
    */
    data = {
        "command": "get_wallet_balance",
    }
    json_data = JSON.stringify(data);
    ws.send(json_data);
}

function get_wallet_balance_response(response) {
    console.log("update balance");
}

async function get_transactions() {
    /*
    Sends websocket request to get transactions
    */
    data = {
        "command": "get_transactions",
    }
    json_data = JSON.stringify(data);
    ws.send(json_data);
}

function get_transactions_response(response) {
    /*
    Called when response is received for get_transactions request
    */
    clean_table()

    for (var i = 0; i < response.txs.length; i++) {
        var tx = JSON.parse(response.txs[i]);
        console.log(tx);
        var row = table.insertRow(0);
        var cell_type = row.insertCell(0);
        var cell_to = row.insertCell(1);
        var cell_date = row.insertCell(2);
        var cell_status = row.insertCell(3);
        var cell_amount = row.insertCell(4);
        var cell_fee = row.insertCell(5);
        //type of transaction
        if (tx["incoming"]) {
            cell_type.innerHTML = "Incoming";
        } else {
            cell_type.innerHTML = "Outgoing";
        }
        // Receiving puzzle hash
        cell_to.innerHTML = tx["to_puzzle_hash"];

        // Date
        var date = new Date(parseInt(tx["created_at_time"]) * 1000);
        cell_date.innerHTML = "" + date;

        // Confirmation status
        if (tx["confirmed"]) {
             index = tx["confirmed_block_index"];
             cell_status.innerHTML = "Confirmed" + green_checkmark +"</br>" + "Block: " + index;
        } else {
             cell_status.innerHTML = "Pending " + red_checkmark;
        }

        // Amount and Fee
        cell_amount.innerHTML = tx["amount"];
        cell_fee.innerHTML = tx["fee_amount"];
    }
}

function clean_table() {
    while (table.rows.length > 0) {
        table.deleteRow(0);
    }
}

clean_table();
