#!/bin/bash
#podman run -it --rm -p 80:80 -v /scripts/element_config.json:/app/config.json docker.io/vectorim/element-web
podman run -it --rm -p 8080:80 -v $PWD/scripts/element_config.json:/app/config.json docker.io/vectorim/element-web
