.PHONY: smoke demo

smoke:
	sudo ./testbed/smoke.sh

demo:
	python -m experiments.runner demo --scenario experiments/scenarios/iso-download.yaml
