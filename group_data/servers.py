role = "server"
# The server also runs the control plane, so it reserves more for Kubernetes itself.
kube_reserved = {"cpu": "200m", "memory": "512Mi"}
