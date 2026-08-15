#!/bin/zsh

set -eu

project_dir="${0:A:h:h}"
env_file="$project_dir/.env"
python_bin="/Users/user/.pyenv/versions/3.12.10/bin/python"

if [[ ! -f "$env_file" ]]; then
    print -u2 "Missing environment file: $env_file"
    exit 1
fi

if [[ ! -x "$python_bin" ]]; then
    print -u2 "Python executable is unavailable: $python_bin"
    exit 1
fi

cd "$project_dir"
set -a
source "$env_file"
set +a

exec "$python_bin" src/search_and_email.py "$@"
