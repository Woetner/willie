# WILL-E — run these on the Mac, inside the willie/ folder.
# Override on the command line if needed:  make deploy PI=woetner@willie.local
PI      ?= willie.local
PI_DIR  ?= willie
RSYNC   := rsync -az --delete \
             --exclude .git/ --exclude .venv/ --exclude __pycache__/ \
             --exclude '*.log' --exclude .DS_Store

.PHONY: help deploy sync setup deps service logs status restart stop ssh pull-config run-local

help:
	@echo "make setup        first time: copy repo + run tools/pi_setup.sh on the Pi (A4)"
	@echo "make deploy       sync code to the Pi + restart willie (A5)"
	@echo "make logs         follow the willie log on the Pi"
	@echo "make status       systemd status of willie"
	@echo "make pull-config  copy the live settings from the Pi into config/willie.yaml"
	@echo "make run-local    run WILL-E on the Mac (dashboard on http://localhost:8080)"

sync:
	$(RSYNC) ./ $(PI):$(PI_DIR)/

setup: sync
	ssh -t $(PI) 'bash $(PI_DIR)/tools/pi_setup.sh'

deps: sync
	ssh $(PI) 'cd $(PI_DIR) && .venv/bin/pip install -q -r requirements.txt'

service: sync
	ssh $(PI) 'sudo bash $(PI_DIR)/tools/install_service.sh'

# requirements.txt changed?  ->  make deps  (deploy does not reinstall packages, to stay fast)
deploy: sync
	ssh $(PI) 'sudo systemctl restart willie'
	@echo "deployed -> http://$(PI):8080"

restart:
	ssh $(PI) 'sudo systemctl restart willie'

stop:
	ssh $(PI) 'sudo systemctl stop willie'

logs:
	ssh -t $(PI) 'journalctl -u willie -f -n 50 -o cat'

status:
	ssh $(PI) 'systemctl status willie --no-pager'

ssh:
	ssh $(PI)

pull-config:
	scp $(PI):.config/willie/willie.yaml config/willie.yaml
	@echo "config/willie.yaml updated — review with git diff, then commit"

run-local:
	test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	WILLIE_CONFIG=.local/willie.yaml WILLIE_DATA=.local .venv/bin/python -m willie
