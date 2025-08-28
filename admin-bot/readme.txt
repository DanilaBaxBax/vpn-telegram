sudo apt-get install -y python3-venv python3-pip
 
 python3 -m venv /opt/vpn-bot/.venv
 source /opt/vpn-bot/.venv/bin/activate
 pip install --upgrade pip
 pip install python-telegram-bot==20.7

chmod +x /root/vpn_conf.sh 
source /opt/vpn-bot/.venv/bin/activate
sudo -E /opt/vpn-bot/.venv/bin/python /root/vpn_bot_admin.py
danilabaxbax@MacBook-Pro-Daniil-2 Desktop % 


