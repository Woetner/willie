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

.PHONY: help sync setup diet deps service deploy restart stop logs status ssh ram link-test ask-camera face face-install voice \
        pull-config run-local fw flash monitor bench-camera bench-screen bench-audio-out voice-pi live-talk improve improve-watch

help:
	@echo "Pi"
	@echo "  make setup        fresh Pi: copy repo + run tools/pi_setup.sh (A4)"
	@echo "  make diet         OS diet: Bluetooth/MQTT/pigpio off, journald in RAM, service limits (A8)"
	@echo "  make deploy       sync code + restart WILL-E (A5)"
	@echo "  make ram          RAM table from the Pi (A8, D21)"
	@echo "  make ask-camera   take one photo and ask Gemini about it (bench prototype)"
	@echo "  make face         say 'Hey Willie' and talk; camera is automatic (bench prototype)"
	@echo "  make face-install install the short 'willie' face-console command on the Pi"
	@echo "  make voice        talk to WILL-E with the MacBook mic + speakers (temporary bridge)"
	@echo "  make link-test    200 pings Pi -> MCU, prints round-trip times (A9)"
	@echo "  make logs         follow the core log"
	@echo "  make pull-config  copy the live settings from the Pi into config/willie.yaml"
	@echo "  make bench-camera B8 camera test, photos land in ../photos/bench/b8/"
	@echo "  make bench-screen B5 screen blink fps + touch test"
	@echo "  make bench-audio-out B6 speaker test (answer the listening questions)"
	@echo "  make voice-pi     hands-free: wake word -> spoken conversation"
	@echo "  make live-talk    one 60 s spoken session, no wake word"
	@echo "Self-improvement (runs Claude Code on the Mac, never deploys)"
	@echo "  make improve       do the changes WILL-E was asked for, on a branch"
	@echo "  make improve-watch keep watching for spoken requests"
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

# On-demand physical UI: Pi mic + speaker + face, with a cloud-routed temporary
# wake detector. The final local wake detector remains B13 on the MCU.
face: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/willie_console.py'

face-install: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/install_face_console.sh'

# stops the core for a moment so the test owns the serial port
link-test:
	ssh $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python -m willie.hal.link -n 200; sudo systemctl start willie'

pull-config:
	scp $(PI):.config/willie/willie.yaml config/willie.yaml
	@echo "config/willie.yaml updated — review with git diff, then commit"

# ---------------------------------------------------------------- Phase B bench tests
# The core is stopped during a test so nothing else owns the camera/screen.
bench-camera: sync
	ssh -t $(PI) 'sudo systemctl stop willie; bash $(PI_DIR)/tools/bench/camera.sh; sudo systemctl start willie'
	mkdir -p ../photos/bench/b8
	scp '$(PI):bench/b8/*.jpg' ../photos/bench/b8/

bench-screen: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && sudo .venv/bin/python tools/bench/screen.py; sudo systemctl start willie'

# Hands-free voice loop. Run from the Pi's console so it owns the microphone.
voice-pi: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/willie_voice.py'

live-talk: sync
	ssh -t $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/live_talk.py 60'

bench-audio-out: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python tools/bench/audio_out.py; sudo systemctl start willie'

# ---------------------------------------------------------------- MCU
fw:
	cd firmware && pio run -e $(MCU)

flash:
	cd firmware && pio run -e $(MCU) -t upload

monitor:
	cd firmware && pio device monitor -e $(MCU)

# Picks up what WILL-E was asked to change and lets Claude Code do it in a
# worktree on a branch. Deploying stays a human decision.
improve:
	bash tools/improve_worker.sh

improve-watch:
	bash tools/improve_worker.sh --watch

# ---------------------------------------------------------------- Mac
# Temporary bench bridge: the Mac records and speaks, the Pi keeps the camera and
# the API key. Not the robot's voice path - that is the Gate G1 adapter (D9).
voice: sync
	test -d .venv || python3 -m venv .venv
	.venv/bin/python -c 'import sounddevice' 2>/dev/null || .venv/bin/pip install -q -r requirements-mac.txt
	.venv/bin/python tools/voice_mac.py

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
