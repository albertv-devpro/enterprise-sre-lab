# -*- mode: ruby -*-
# vi: set ft=ruby :

SERVERS = {
  "web01" => {
    box: "ubuntu/jammy64",
    ip: "192.168.56.30",
    cpus: 1,
    memory: 1024
  },
  "app01" => {
    box: "generic/rocky9",
    ip: "192.168.56.31",
    cpus: 2,
    memory: 2048
  },
  "db01" => {
    box: "generic/rocky9",
    ip: "192.168.56.32",
    cpus: 1,
    memory: 1024
  },
  "monitoring01" => {
    box: "ubuntu/jammy64",
    ip: "192.168.56.28",
    cpus: 2,
    memory: 2048
  }
}.freeze

Vagrant.configure("2") do |config|
  config.vm.boot_timeout = 900

  config.hostmanager.enabled = true
  config.hostmanager.manage_host = true
  config.hostmanager.ignore_private_ip = false
  config.hostmanager.include_offline = true

  SERVERS.each do |name, opts|
    config.vm.define name do |node|
      node.vm.box = opts[:box]
      node.vm.hostname = name
      node.vm.network "private_network", ip: opts[:ip]

      node.vm.provider "virtualbox" do |vb|
        vb.name = "enterprise-sre-lab-#{name}"
        vb.cpus = opts[:cpus]
        vb.memory = opts[:memory]
        vb.gui = false
        vb.customize ["modifyvm", :id, "--natdnshostresolver1", "on"]
        vb.customize ["modifyvm", :id, "--ioapic", "on"]
      end
    end
  end
end
