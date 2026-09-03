SHELL := /bin/sh

PYTHON ?= python3
NPM ?= npm

.PHONY: test test-extension test-native-host test-server check package-extension help

help:
	@echo 'make test             运行扩展、Native Host、服务端的全部测试'
	@echo 'make test-extension   只运行 Edge 扩展测试'
	@echo 'make test-native-host 只运行 Native Messaging Host 测试'
	@echo 'make test-server      只运行服务端测试'
	@echo 'make check            运行 Python 语法检查和全部测试'
	@echo 'make package-extension 打包可安装的 Edge 扩展 ZIP'

test: test-extension test-native-host test-server

test-extension:
	@test -f extension/package.json || { echo '缺少 extension/package.json'; exit 1; }
	@cd extension && $(NPM) test

test-native-host:
	@test -d native-host || { echo '缺少 native-host/'; exit 1; }
	@$(PYTHON) -m unittest discover -s native-host -p 'test_*.py' -v

test-server:
	@test -d server || { echo '缺少 server/'; exit 1; }
	@$(PYTHON) -m unittest discover -s server/tests -p 'test_*.py' -v

check:
	@PYTHONPYCACHEPREFIX="$(CURDIR)/.cache/pycache" $(PYTHON) -m compileall -q native-host server
	@$(MAKE) test

package-extension:
	@./scripts/package-extension.sh
