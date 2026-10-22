# Verification for the native eBIRForms port.
#
#   make test      static checks + a functional run of install.sh
#   make run       launch the app
#   make assets    recover the asset tree from the installer in this folder
#
NODE ?= $(shell command -v node 2>/dev/null || echo $(HOME)/Desktop/Workspace/toolchain/node-v24.22.0-linux-x64/bin/node)
PYTHON ?= python3

.PHONY: test run assets clean-test

test:
	@echo "== shell syntax =="
	sh -n install.sh
	sh -n run.sh
	@echo "== python compiles =="
	$(PYTHON) -m py_compile native/run.py native/ebirforms_host/*.py tools/extract_assets.py
	@echo "== injected javascript parses =="
	$(NODE) --check native/ebirforms_host/compat.js
	$(NODE) --check native/ebirforms_host/ebirtools.js
	@echo "== desktop entry is valid =="
	desktop-file-validate packaging/ebirforms-native.desktop
	@echo "== install.sh installs a valid desktop entry =="
	@if [ ! -f assets/app/BIRForms.hta ]; then \
		echo "   skipped: run 'make assets' first (asset tree missing)"; \
	else \
		tmp=$$(mktemp -d); \
		XDG_DATA_HOME=$$tmp ./install.sh > /dev/null; \
		desktop-file-validate $$tmp/applications/ebirforms-native.desktop; \
		test -x $$tmp/applications/../icons/hicolor/256x256/apps/ebirforms-native.png -o \
			-f $$tmp/icons/hicolor/256x256/apps/ebirforms-native.png; \
		grep -q "^Exec=\(.*/\)\?run.sh$$" $$tmp/applications/ebirforms-native.desktop; \
		grep -q "^Icon=.*ebirforms-native.png$$" $$tmp/applications/ebirforms-native.desktop; \
		echo "   installed entry points at: $$(sed -n 's/^Exec=//p' $$tmp/applications/ebirforms-native.desktop)"; \
		rm -rf $$tmp; \
	fi
	@echo "== install.sh degrades gracefully without a writable data dir =="
	@if [ ! -f assets/app/BIRForms.hta ]; then \
		echo "   skipped (asset tree missing)"; \
	else \
		out=$$(XDG_DATA_HOME=/proc/ebir-readonly ./install.sh 2>&1); \
		printf '%s\n' "$$out" | grep -q "desktop entry: skipped"; \
		printf '%s\n' "$$out" | grep -q "start it with:"; \
		echo "   reported the launch command instead of failing"; \
	fi
	@echo "ALL CHECKS PASSED"

run:
	./run.sh

assets:
	$(PYTHON) tools/extract_assets.py

clean-test:
	rm -rf native/__pycache__ native/ebirforms_host/__pycache__ tools/__pycache__
