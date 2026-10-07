# k3s via the native NixOS module, enabled only in the configure phase (Req 3.8, 5-8, 11, 13, 16).
{
  lib,
  pkgs,
  node,
  ...
}:
let
  k = node.k3s;
  kl = k.kubelet;
  isServer = k.role == "server";

  # Agent Substrate (podcertificate controller) needs these off-by-default APIs/features (Req 16.4);
  # mirrors the cluster-wide featureGates/runtimeConfig in Substrate's kind setup.
  substrateGates = "ClusterTrustBundle=true,ClusterTrustBundleProjection=true,PodCertificateRequest=true";

  kubeletConfig = {
    apiVersion = "kubelet.config.k8s.io/v1beta1";
    kind = "KubeletConfiguration";
    failSwapOn = false;
    memorySwap.swapBehavior = kl.swapBehavior;
    systemReserved = kl.systemReserved;
    kubeReserved = kl.kubeReserved;
    enforceNodeAllocatable = [ "pods" ];
    evictionHard = kl.evictionHard;
    evictionSoft = kl.evictionSoft;
    evictionSoftGracePeriod = kl.evictionSoftGracePeriod;
    evictionPressureTransitionPeriod = "30s";
    kernelMemcgNotification = kl.kernelMemcgNotification;
    imageGCHighThresholdPercent = kl.imageGcHigh;
    imageGCLowThresholdPercent = kl.imageGcLow;
    serializeImagePulls = false;
    maxParallelImagePulls = 4;
    cpuManagerPolicy = kl.cpuManagerPolicy;
    # Kubernetes 1.36 enforces gate dependencies: ClusterTrustBundleProjection needs
    # ClusterTrustBundle. On the server it was inherited from the apiserver flag (one k3s
    # process); an agent's kubelet panics without it, so set it explicitly everywhere.
    featureGates = {
      ClusterTrustBundle = true;
      ClusterTrustBundleProjection = true;
      PodCertificateRequest = true;
    };
  }
  // lib.optionalAttrs (kl.reservedCpus != null) { reservedSystemCPUs = kl.reservedCpus; };

  # k3s writes its own kubelet defaults as drop-in 00-k3s-defaults.conf, and drop-ins override a
  # --config file. So ours is a drop-in too, ordered last. (services.k3s.extraKubeletConfig uses
  # --kubelet-arg=config=, which those defaults could silently override.) JSON is valid YAML.
  kubeletDropIn = pkgs.writeText "99-kube-ax.conf" (builtins.toJSON kubeletConfig);
  kubeletDropInDir = "/var/lib/rancher/k3s/agent/etc/kubelet.conf.d";
in
lib.mkIf (node.phase == "k3s") {
  services.k3s = {
    enable = true;
    # nixos-26.05 defaults to 1.35; Substrate supports latest stable (1.37) and previous minor.
    package = pkgs.k3s_1_36;
    role = k.role;
    tokenFile = k.tokenFile;
    serverAddr = lib.mkIf (!isServer) k.serverAddr;
    nodeIP = k.nodeIp;
    nodeLabel = lib.mapAttrsToList (name: value: "${name}=${value}") k.labels;
    disable = lib.mkIf isServer k.disable;
    gracefulNodeShutdown.enable = true;

    extraFlags = lib.optionals isServer (
      [
        "--advertise-address=${k.nodeIp}"
        "--tls-san=${k.nodeIp}"
        "--flannel-backend=${k.flannelBackend}"
        "--disable-cloud-controller"
        "--write-kubeconfig-mode=0600"
        "--kube-apiserver-arg=feature-gates=${substrateGates}"
        "--kube-apiserver-arg=runtime-config=certificates.k8s.io/v1beta1=true"
        "--kube-controller-manager-arg=feature-gates=${substrateGates}"
        # Delete failed/evicted pods beyond 100 (default 12500). On 2026-10-05 a disk-pressure storm
        # left ~3000 evicted kube-state-metrics pods; kubelet churned on them at 300 % CPU.
        "--kube-controller-manager-arg=terminated-pod-gc-threshold=100"
      ]
    );

    # Server-only auto-deployed manifests: single-entry-IP ingress and batch priorities.
    manifests = lib.mkIf isServer {
      traefik-config.content = {
        apiVersion = "helm.cattle.io/v1";
        kind = "HelmChartConfig";
        metadata = {
          name = "traefik";
          namespace = "kube-system";
        };
        # valuesContent is YAML; JSON is valid YAML.
        spec.valuesContent = builtins.toJSON {
          deployment.replicas = k.traefikReplicas;
          nodeSelector."kubernetes.io/hostname" = node.hostName;
        };
      };
      priority-classes.content = [
        {
          apiVersion = "scheduling.k8s.io/v1";
          kind = "PriorityClass";
          metadata.name = "batch-low";
          value = 1000;
          preemptionPolicy = "Never";
          description = "Batch jobs: evicted first under pressure, never preempt others.";
        }
        {
          apiVersion = "scheduling.k8s.io/v1";
          kind = "PriorityClass";
          metadata.name = "service-default";
          value = 10000;
          description = "Long-running services.";
        }
      ];
    };
  };

  systemd.tmpfiles.rules = [
    "d ${kubeletDropInDir} 0755 root root -"
    "L+ ${kubeletDropInDir}/99-kube-ax.conf - - - - ${kubeletDropIn}"
  ];

  # Substrate's node directory (actor image cache, task /workspace scratch) lives on the large
  # containerd volume instead of the 20 GiB OS volume (measured 13 GiB of image cache on 2026-10-05).
  # Kubelet's imagefs thresholds watch that volume, so images and scratch share one guarded budget.
  system.activationScripts.ateDir = lib.mkIf (k.ateOnContainerd or false)
    "mkdir -p /var/lib/rancher/k3s/agent/containerd/ate /var/lib/ate";
  fileSystems."/var/lib/ate" = lib.mkIf (k.ateOnContainerd or false) {
    device = "/var/lib/rancher/k3s/agent/containerd/ate";
    fsType = "none";
    options = [
      "bind"
      "x-systemd.requires-mounts-for=/var/lib/rancher/k3s/agent/containerd"
    ];
  };

  # Keep the node agent responsive when jobs saturate CPU/memory/IO (Req 13.8).
  # Go returns memory lazily; a soft cap plus a more eager GC keeps k3s's footprint near its working set
  # (measured before: server ~1.1 GiB RSS). Soft limit: above it Go collects more often, never OOMs.
  systemd.services.k3s.environment = {
    GOGC = "50";
    GOMEMLIMIT = if isServer then "800MiB" else "256MiB";
  };

  systemd.services.k3s.serviceConfig = {
    OOMScoreAdjust = -999;
    CPUWeight = 1000;
    IOWeight = 1000;
  };

  networking.firewall = {
    allowedTCPPorts = [ 10250 ] ++ lib.optionals isServer [ 6443 80 443 ];
    allowedUDPPorts = lib.optional (k.flannelBackend == "vxlan") 8472;
    trustedInterfaces = [
      "cni0"
      "flannel.1"
    ];
    # host-gw routes pod subnets via node IPs on the primary NIC; strict rpfilter can drop them.
    checkReversePath = "loose";
  };
}
