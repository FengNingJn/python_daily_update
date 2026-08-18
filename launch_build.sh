#!/bin/sh
nohup /bin/sh /volume1/docker/nga-update/build_start.sh \
  >>/volume1/docker/nga-update/logs/build.log 2>&1 &
