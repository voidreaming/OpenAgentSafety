#!/bin/sh
set -e

########## PRE INIT PHASE ############

# Use synthetic service hostname, the-agent-company.com in tasks and point it
# to the real service host. Prefer getent; ping may be unavailable in runtime image.
TARGET_HOST="${SERVER_HOSTNAME:-localhost}"
SERVICE_IP=""
if command -v getent >/dev/null 2>&1; then
    SERVICE_IP=$(getent ahostsv4 "$TARGET_HOST" | awk 'NR==1{print $1}')
fi
if [ -z "$SERVICE_IP" ] && command -v ping >/dev/null 2>&1; then
    SERVICE_IP=$(ping -c 1 "$TARGET_HOST" | grep PING | awk -F'[()]' '{print $2}')
fi
if [ -z "$SERVICE_IP" ]; then
    SERVICE_IP="127.0.0.1"
fi
if ! grep -q '[[:space:]]the-agent-company\.com$' /etc/hosts 2>/dev/null; then
    echo "$SERVICE_IP the-agent-company.com" >> /etc/hosts
fi

echo "Resetting services..."
bash /utils/reset.sh

# Assumed there is no pre_init.{sh, py}
# if [ -f "/utils/pre_init.sh" ]; then
#     bash /utils/pre_init.sh
# fi

# if [ -f "/utils/pre_init.py" ]; then
#     python_default /utils/pre_init.py
# fi
######################################

########## RUN INITIALIZATION ########
# set up task-specific NPC ENV, only if NPC is required
# FIXME: This is handled via fast-api server now
# if [ -f "/npc/scenarios.json" ]; then
#     python_default /npc/run_multi_npc.py
# fi

# # populate task-specific data if applicable
# if [ -f "/utils/populate_data.py" ]; then
#     python_default /utils/populate_data.py
# fi
######################################

########## POST INIT PHASE ###########
# assume there is no post_init.{sh, py}
# if [ -f "/utils/post_init.sh" ]; then
#     bash /utils/post_init.sh
# fi

# if [ -f "/utils/post_init.py" ]; then
#     python_default /utils/post_init.py
# fi
######################################
