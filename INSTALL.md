## Installation

To install the chia-blockchain node, follow the instructions according to your operating system.
After installing, follow the remaining instructions in [README.md](README.md) to run the software.

### MacOS
Currently Catalina (10.15.x) is required. Make sure [brew](https://brew.sh/) is available before starting the setup.
```bash
brew upgrade python
brew install npm gmp

git clone https://github.com/Chia-Network/chia-blockchain.git
cd chia-blockchain

sh install.sh
. ./activate
```

### Debian/Ubuntu

Install dependencies for Ubuntu 18.04, Ubuntu 19.x or newer.
```bash
sudo apt-get update
sudo apt-get install python3-venv git -y

# Either checkout the source and install
git clone https://github.com/Chia-Network/chia-blockchain.git
cd chia-blockchain

sh install.sh

. ./activate

# Or install chia-blockchain as a package
python3 -m venv venv
ln -s venv/bin/activate
. ./activate
pip install --upgrade pip
pip install -i https://hosted.chia.net/simple/ miniupnpc==0.1.dev5 setproctitle==1.1.10 cbor2==5.0.1

pip install chia-blockchain==1.0.beta2

. /activate
```

### Windows (WSL)
#### Install WSL2 + Ubuntu 18.04 LTS

From an Administrator PowerShell
`dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart`
and then
`dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart`.
This requires a reboot at this point. Once that is complete, install Ubuntu 18.04 LTS from the Microsoft Store and run it. Then follow the steps below.
```bash
sudo apt-get -y update
sudo apt-get -y upgrade

sudo apt-get install python3.7-venv python3-pip -y

python3.7 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -i https://hosted.chia.net/simple/ miniupnpc==0.1.dev5 setproctitle==1.1.10 cbor2==5.0.1
pip install chia-blockchain==1.0.beta2
```
You will need to download the Windows native Wallet and unzip into somewhere convenient in Windows.

[Download: chia-win32-x64.zip](https://hosted.chia.net/beta-1.0-win64-wallet/chia-win32-x64.zip)

Instead of `chia-start-wallet-ui &` as explained in the [README.md](README.md) you run `chia-start-wallet-server &` in Ubuntu/WSL 2 to allow the Wallet to connect to the Full Node running in Ubuntu/WSL 2. Once you've enabled `chia-start-wallet-server &` you can run `chia.exe` from the unzipped `chia-win32-x64` directory.

### Amazon Linux 2

```bash
sudo yum update
sudo yum install python3 python3-devel git

# Install npm and node
curl -sL https://rpm.nodesource.com/setup_10.x | sudo bash -
sudo yum install nodejs

# uPnP and setproctitle require compiling
sudo yum install gcc

git clone https://github.com/Chia-Network/chia-blockchain.git
cd chia-blockchain

sh install.sh

. ./activate
```

### CentOS 7.7 or newer

```bash
sudo yum update
sudo yum install gcc openssl-devel bzip2-devel libffi libffi-devel
sudo yum install libsqlite3x-devel

# Install python 3.7
wget https://www.python.org/ftp/python/3.7.7/Python-3.7.7.tgz
tar -zxvf Python-3.7.7.tgz ; cd Python-3.7.7
./configure --enable-optimizations; sudo make install; cd ..

# Install npm and node
curl -sL https://rpm.nodesource.com/setup_10.x | sudo bash -
sudo yum install nodejs

git clone https://github.com/Chia-Network/chia-blockchain.git
cd chia-blockchain

sh install.sh
. ./activate
```
