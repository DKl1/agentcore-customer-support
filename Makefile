# Requires: Python 3.12, Node 20+ (CDK CLI), Docker with buildx (linux/arm64), AWS credentials (SSO).
# On Windows run from Git Bash / WSL, or copy the commands.
STAGE ?= dev
OUTPUTS := .deploy/outputs.json
CDK := cd infra && npx aws-cdk@2

.PHONY: install lint test synth deploy configure seed e2e evidence destroy

install:
	python -m pip install -r requirements-dev.txt -r src/agent/requirements.txt -r infra/requirements.txt

lint:
	ruff check src client scripts tests infra
	ruff format --check src client scripts tests infra
	mypy src/agent/support_agent src/tools/support_tools

test:
	pytest

synth:
	$(CDK) synth -c stage=$(STAGE)

# First deploy: CDK -> Identity provider -> Cedar policy engine -> seed data.
# Afterwards, pin the policy engine: make deploy POLICY_ENGINE_ARN=arn:...
deploy:
	mkdir -p .deploy
	$(CDK) deploy -c stage=$(STAGE) $(if $(POLICY_ENGINE_ARN),-c policyEngineArn=$(POLICY_ENGINE_ARN)) \
		--require-approval broadening --outputs-file ../$(OUTPUTS)
	$(MAKE) configure seed

configure:
	python scripts/configure_runtime.py --stage $(STAGE)
	python scripts/configure_identity.py --stage $(STAGE)
	python scripts/configure_policy.py --stage $(STAGE) --mode ENFORCE

seed:
	python scripts/seed_data.py --stage $(STAGE) --reset-refunds

e2e:
	E2E=1 STAGE=$(STAGE) pytest -m e2e tests/e2e -v

evidence:
	python scripts/collect_evidence.py --stage $(STAGE)

destroy:
	$(CDK) destroy -c stage=$(STAGE)
