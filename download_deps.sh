# Galaxy 2.1+ runs plugins with 64-bit Python 3.13, so pip must also run on Python 3.13
python3.13 -m pip install -r ./requirements.txt --platform win_amd64 --python-version 3.13 --implementation cp --only-binary=:all: --target ./3rdparty_windows --no-compile --upgrade
