# Enterprise SRE & Infrastructure Automation Lab

## 🎯 Project Goals
- Master Infrastructure as Code (IaC) and configuration management using Ansible.
- Deploy and configure enterprise monitoring with Prometheus and Grafana.
- Automate hybrid environments (Ubuntu & Rocky Linux) mirroring production systems.
- Build self-service provisioning workflows and API integrations.

## 📚 Modules & Progress Tracker
- [ ] **Module 1:** Environment Setup & Multi-Node Vagrant Provisioning (`Vagrantfile` + `inventory.yml`)
- [ ] **Module 2:** Configuration Management & Automation via Ansible (Node Exporter deployment)
- [ ] **Module 3:** Observability Stack Deployment (Prometheus server & Grafana dashboards)
- [ ] **Module 4:** CI/CD & Self-Service Workflows (Python, REST APIs, and automated change tickets)
- [ ] **Module 5:** VMware vSphere & Ceph Storage Reliability Engineering

## Running Ansible from WSL

Ansible is installed in a Python virtual environment in Ubuntu 24.04 WSL. Open that
distribution and start from the project directory:

```bash
cd $PROJECT_DIR
ansible all -i inventory.yml -m ping
```

Vagrant SSH keys are copied into `~/.ssh/vagrant-enterprise-sre-lab` with Linux-only
permissions. If a VM is destroyed and recreated, refresh the copies from this
directory because Vagrant may generate a new key:

```bash
mkdir -p ~/.ssh/vagrant-enterprise-sre-lab
chmod 700 ~/.ssh ~/.ssh/vagrant-enterprise-sre-lab
for host in web01 app01 db01 monitoring01; do
  install -m 600 ".vagrant/machines/$host/virtualbox/private_key" \
    "$HOME/.ssh/vagrant-enterprise-sre-lab/$host"
done
```

The inventory disables SSH host-key checking for these disposable local Vagrant
guests, whose host keys change when they are recreated. Do not use that setting
for production systems. Ansible must be run inside WSL; Windows-mounted project
directories are world-writable from WSL, so Ansible ignores `ansible.cfg` files
in this directory. Keep connection settings in the inventory or use a config
file in the WSL Linux filesystem.
