#!/bin/env bash

GMAIL_BRIDGE_CONFIG_PATH="./dev_config.yaml" python3 -m app hs_config > scripts/synapse_configs/gmail.yaml
