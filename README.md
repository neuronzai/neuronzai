# Neuronz.ai plugin marketplace

The [Neuronz.ai](https://neuronz.ai) plugin for Claude Code and omp: profile-scoped
persistent memory for your coding agent, backed by the managed Neuronz.ai service.

This repository is generated from the Neuronz.ai release pipeline (plugin
v0.25.123). It holds only the plugin; please do not open pull requests here.

## Claude Code

```bash
claude plugin marketplace add https://github.com/neuronzai/neuronzai.git
claude plugin install neuronzai@neuronzai
```

Then start a new session and run `/neuronzai:login`.

## omp

```bash
omp plugin marketplace add https://github.com/neuronzai/neuronzai.git
omp plugin install neuronzai@neuronzai
```

Then start a new omp session and run `/neuronzai:login`.

Full setup, updates and troubleshooting: https://docs.neuronz.ai/guide/connect/
