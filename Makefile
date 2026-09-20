# WILL-E — run these on the Mac, inside the willie/ folder.
# Override on the command line if needed:  make deploy PI=woetner@willie.local
PI      ?= willie.local
PI_DIR  ?= willie
# ESP32 board for the firmware (A9): esp32s3 = the planned board (D18),
# esp32dev = the 30-pin ESP32-WROOM DevKit for bench tests.
MCU     ?= esp32s3
RSYNC   := rsync -az --delete \
             --exclude .git/ --exclude .env --exclude .venv/ --exclude __pycache__/ \
             --exclude '*.log' --exclude .DS_Store --exclude firmware/.pio/ --exclude .local/

.PHONY: help sync setup diet deps service deploy restart stop logs status ssh ram link-test ask-camera face \
        pull-config run-local fw flash monitor

help:
	@echo "Pi"
	@echo "  make setup        fresh Pi: copy repo + run tools/pi_setup.sh (A4)"
	@echo "  make diet         OS diet: Bluetooth/MQTT/pigpio off, journald in RAM, service limits (A8)"
	@echo "  make deploy       sync code + restart WILL-E (A5)"
	@echo "  make ram          RAM table from the Pi (A8, D21)"
	@echo "  make ask-camera   take one photo and ask Gemini about it (bench prototype)"
	@echo "  make face         fullscreen face + typed camera questions on the Pi"
	@echo "  make link-test    200 pings Pi -> MCU, prints round-trip times (A9)"
	@echo "  make logs         follow the core log"
	@echo "  make pull-config  copy the live settings from the Pi into config/willie.yaml"
	@echo "MCU (needs PlatformIO on the Mac: brew install platformio)"
	@echo "  make fw           build the firmware            (MCU=$(MCU))"
	@echo "  make flash        build + flash over USB        (MCU=$(MCU))"
	@echo "  make monitor      USB serial monitor (debug output)"
	@echo "Mac"
	@echo "  make run-local    core + dashboard + fake MCU on the Mac -> http://localhost:8080"

# ---------------------------------------------------------------- Pi
sync:
	$(RSYNC) ./ $(PI):$(PI_DIR)/

setup: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/pi_setup.sh'

diet: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/os_diet.sh'

deps: sync
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/pip install -q -r requirements.txt'

service: sync
	ssh $(PI) 'sudo bash $(PI_DIR)/tools/install_service.sh'

# requirements.txt changed?  ->  make deps  (deploy does not reinstall packages, to stay fast)
deploy: sync
	ssh $(PI) 'sudo systemctl restart willie; sudo systemctl stop willie-dashboard || true'
	@echo "deployed -> http://$(PI):8080 (dashboard starts on the first visit)"

restart:
	ssh $(PI) 'sudo systemctl restart willie'

stop:
	ssh $(PI) 'sudo systemctl stop willie'

logs:
	ssh -t $(PI) 'journalctl -u willie -f -n 50 -o cat'

status:
	ssh $(PI) 'systemctl status willie willie-dashboard.socket --no-pager'

ssh:
	ssh $(PI)

ram:
	ssh $(PI) 'bash $(PI_DIR)/tools/ram.sh'

# An on-demand bench tool: preserves the Pi-only .env (and its API key) during sync.
# It prompts for the question on the Pi's attached keyboard.
ask-camera: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/ask_camera.py'

# On-demand physical UI. Run from the Pi's console for its attached keyboard.
face: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/willie_console.py'

# stops the core for a moment so the test owns the serial port
link-test:
	ssh $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python -m willie.hal.link -n 200; sudo systemctl start willie'

pull-config:
	scp $(PI):.config/willie/willie.yaml config/willie.yaml
	@echo "config/willie.yaml updated — review with git diff, then commit"

# ---------------------------------------------------------------- MCU
fw:
	cd firmware && pio run -e $(MCU)

flash:
	cd firmware && pio run -e $(MCU) -t upload

monitor:
	cd firmware && pio device monitor -e $(MCU)

# ---------------------------------------------------------------- Mac
run-local:
	test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	mkdir -p .local
	test -f .local/willie.yaml || sed 's#/dev/serial0#.local/mcu#' config/willie.yaml > .local/willie.yaml
	trap 'kill $$(jobs -p) 2>/dev/null' EXIT INT TERM; \
	.venv/bin/python tools/fake_mcu.py .local/mcu & \
	sleep 1; \
	WILLIE_CONFIG=.local/willie.yaml WILLIE_DATA=.local .venv/bin/python -m willie & \
	WILLIE_CONFIG=.local/willie.yaml WILLIE_DATA=.local .venv/bin/python -m willie.dashboard --port 8080
