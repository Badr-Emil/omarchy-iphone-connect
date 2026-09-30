#!/usr/bin/env bash
# Collects a diagnosis for bug reports. Phone numbers and addresses are masked;
# pass --unmask to keep them.
exec "$(dirname "$(readlink -f "$0")")/../backend/iphone-connect" diagnostics "$@"
