#!/bin/bash
# One-shot setup for an Ubuntu EC2 instance (GPU or CPU). Paste into "User data" when launching,
# or run with: sudo bash bootstrap.sh
# Installs Strands Decider 2B + this app and starts both as services:
#   decider      -> http://127.0.0.1:8099 (local only)
#   no-low-blows -> http://<public-ip>:8080 (open this port to your IP only)
# Progress log: /var/log/no-low-blows-setup.log
set -euxo pipefail
exec > >(tee -a /var/log/no-low-blows-setup.log) 2>&1

APP_USER=ubuntu
HOME_DIR=/home/$APP_USER
REPO_URL=${REPO_URL:-https://github.com/grand1llusion/no-low-blows}
DECIDER_MODEL=${DECIDER_MODEL:-StrandsAgents/strands-decider-2B-qwen3.5-v1-2610}
REGION=${AWS_REGION:-us-east-1}
BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.amazon.nova-pro-v1:0}

if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then DEVICE=cuda; EXTRA="[cuda]"; else DEVICE=cpu; EXTRA=""; fi

apt-get update -y
apt-get install -y python3-venv python3-pip git

sudo -u $APP_USER -H bash <<EOS
set -euxo pipefail
cd $HOME_DIR
[ -d no-low-blows ] && git -C no-low-blows pull --ff-only || git clone $REPO_URL no-low-blows
[ -d strands-decider ] || git clone --depth 1 https://github.com/strands-labs/strands-decider
python3 -m venv venv
venv/bin/pip install -U pip
if [ "$DEVICE" = cpu ]; then venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu; fi
venv/bin/pip install -e "$HOME_DIR/strands-decider$EXTRA"
venv/bin/pip install -r no-low-blows/requirements.txt
EOS

cat > /etc/systemd/system/decider.service <<EOF
[Unit]
Description=Strands Decider 2B server
After=network-online.target
[Service]
User=$APP_USER
WorkingDirectory=$HOME_DIR
ExecStart=$HOME_DIR/venv/bin/strands-decider serve $DECIDER_MODEL --device $DEVICE --port 8099
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/no-low-blows.service <<EOF
[Unit]
Description=No Low Blows web app
After=decider.service
[Service]
User=$APP_USER
WorkingDirectory=$HOME_DIR/no-low-blows
Environment=AWS_REGION=$REGION
Environment=BEDROCK_MODEL_ID=$BEDROCK_MODEL_ID
Environment=STRANDS_DECIDER_URL=http://127.0.0.1:8099
ExecStart=$HOME_DIR/venv/bin/uvicorn app.server:app --host 0.0.0.0 --port 8080
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now decider.service no-low-blows.service
echo "SETUP DONE (device=$DEVICE). The decider downloads its weights on first start; watch: journalctl -u decider -f"
