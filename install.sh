#!/bin/bash
set -e

if [ `uname` = "Linux" ] && type apt-get; then
    # Debian/Ubuntu
    sudo apt-get install -y libgmp3-dev libboost-dev libboost-system-dev npm python3-dev cmake
fi

python3 -m venv venv
ln -s venv/bin/activate
. ./activate
# pip 20.x+ supports Linux binary wheels
pip install --upgrade pip
pip install wheel
pip install -e .
pip install -r requirements.txt

cd ./src/electron-ui
npm install

echo ""
echo "Chia blockchain install.sh complete."
echo "For assistance join us on Keybase in the #testnet chat channel"
echo "https://keybase.io/team/chia_network.public"
echo ""
echo "Return to the README.md to start running the Chia blockchain"
echo "https://github.com/Chia-Network/chia-blockchain/blob/master/README.md"
