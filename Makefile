# WILL-E — run these on the Mac, inside the willie/ folder.
# Override on the command line if needed:  make deploy PI=woetner@willie.local
PI      ?= willie.local
PI_DIR  ?= willie
# ESP32 board for the firmware (D18): the 30-pin ESP32-WROOM DevKit (esp32dev).
MCU     ?= esp32dev
# Run a command on the Pi with the mic to itself: pause the hands-free voice service,
# and start it again afterwards - also after Ctrl-C.
MIC = sudo systemctl stop willie-voice 2>/dev/null; trap "sudo systemctl start willie-voice 2>/dev/null" EXIT;
RSYNC   := rsync -az --delete \
             --exclude .git/ --exclude .env --exclude .venv/ --exclude __pycache__/ \
             --exclude '*.log' --exclude .DS_Store --exclude firmware/.pio/ --exclude .local/

.PHONY: help sync setup diet deps service deploy restart stop logs status ssh ram link-test ask-camera face face-install voice \
        pull-config run-local fw flash monitor bench-mcu bench-camera bench-screen bench-audio-out voice-pi live-talk voices voice-samples wake-record wake-fetch voice-logs wake-test improve improve-watch improve-install

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
	@echo "  make voice-pi     hands-free loop in the foreground (the willie-voice service does this at boot)"
	@echo "  make voice-logs   follow what the hands-free service hears and does"
	@echo "  make wake-test    2 min wake-word bench (S=600 for longer), service paused meanwhile"
	@echo "  make live-talk    spoken session until Ctrl-C, no wake word (S=60 for a timed one)"
	@echo "  make voice-samples make the candidate voice samples on the Pi (V='Orus Schedar' for only those)"
	@echo "  make voices       play the voice samples one after another on the Mac"
	@echo "  make wake-record  record 'Hey Willie' + everyday sound through the robot's mic (D2 round 2)"
	@echo "  make wake-fetch   copy those recordings to the Mac training workspace"
	@echo "Self-improvement (runs Claude Code on the Mac, never deploys)"
	@echo "  make improve       do the changes WILL-E was asked for, on a branch"
	@echo "  make improve-watch keep watching for spoken requests"
	@echo "  make improve-install  run the watcher in the background at login (launchd)"
	@echo "MCU (needs PlatformIO on the Mac: brew install platformio)"
	@echo "  make fw           build the firmware            (MCU=$(MCU))"
	@echo "  make flash        build + flash over USB        (MCU=$(MCU))"
	@echo "  make monitor      USB serial monitor (debug output)"
	@echo "  make bench-mcu T=servo|sensors|io|motors|spin|watch   B9-B12 tests over the link"
	@echo "  make face-demo    on the Pi: 60 s face-only demo + RAM/render measurements"
	@echo "Mac"
	@echo "  make face-preview animated face studio -> http://127.0.0.1:8765"
	@echo "  make face-test    offline face + voice regression checks"
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
# wake detector. The final local wake detector is D2 (on the Pi, D19).
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
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/willie_voice.py'

voice-logs:
	ssh -t $(PI) 'journalctl -u willie-voice -f -n 30 -o cat'

wake-test: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/bench/wake.py $(or $(S),120)'

live-talk: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/live_talk.py $(S)'

# Wake word round 2 (D2): record Wouter through the robot's mic, fetch for training on the Mac.
wake-record: sync
	ssh -t $(PI) '$(MIC) cd $(PI_DIR) && .venv/bin/python tools/wakeword/record.py'

wake-fetch:
	mkdir -p .local/wakeword/samples/wouter .local/wakeword/samples/wouter_neg_long
	rsync -a $(PI):$(PI_DIR)/.local/wakeword_rec/positive/ .local/wakeword/samples/wouter/
	rsync -a $(PI):$(PI_DIR)/.local/wakeword_rec/negative/ .local/wakeword/samples/wouter_neg_long/
	@echo "$$(ls .local/wakeword/samples/wouter | wc -l) positives, $$(ls .local/wakeword/samples/wouter_neg_long | wc -l) long negatives"

# Voice audition (D7): samples are made on the Pi (API key) and played on the Mac.
voice-samples: sync
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/python tools/voice_samples.py $(V)'

voices:
	mkdir -p .local/voices && rm -f .local/voices/*.wav
	rsync -a $(PI):$(PI_DIR)/.local/voices/ .local/voices/
	@for f in $$(ls .local/voices/*.wav | sort -t/ -k3 -n); do \
	  n=$$(basename $$f .wav); echo "> $${n#*_}"; afplay $$f; sleep 1; done

bench-audio-out: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python tools/bench/audio_out.py; sudo systemctl start willie'

# ---------------------------------------------------------------- MCU
fw:
	cd firmware && pio run -e $(MCU)

# B9–B12 over the link, e.g. make bench-mcu T=servo  (servo | sensors | io | motors | spin | watch)
T ?= watch
bench-mcu: sync
	ssh -t $(PI) 'sudo systemctl stop willie; cd $(PI_DIR) && .venv/bin/python tools/bench/mcu.py $(T); sudo systemctl start willie'

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

improve-install:
	bash tools/install_improve_worker.sh

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

# D6: offline preview and face-only hardware demo. Neither opens mic/camera.
.PHONY: face-preview face-demo face-test
face-preview:
	python3 tools/face_preview.py

face-demo:
	.venv/bin/python tools/bench/face.py --seconds 60

face-test:
	.venv/bin/python -m pytest tests/test_face.py tests/test_voice_adapter.py -q
