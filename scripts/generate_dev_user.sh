#!/bin/env bash
set -e 
echo "Generating users"
scripts/create_user.sh -u dev -p dev --no-admin
echo "user: dev, password: dev"
scripts/create_user.sh -u dev2 -p dev --no-admin
echo "user: dev2, password: dev"
scripts/create_user.sh -u dev3 -p dev --no-admin
echo "user: dev3, password: dev"
