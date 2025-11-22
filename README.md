# 🚀 DevOps Project: Automated Deployment on AWS with Kubernetes & GitOps

This project demonstrates a **full-stack DevOps pipeline** simulating a real production environment. It provisions AWS infrastructure using Terraform, sets up a **multi-node Kubernetes cluster with Kind**, and implements a **GitOps workflow using ArgoCD** to deploy a containerized Python Flask microservice.  
It also includes a complete **observability stack** with Prometheus and Grafana.

## 🏗 Architecture Overview

| Component | Technology |
|----------|------------|
| **Cloud Infrastructure** | AWS EC2 (Ubuntu) |
| **Infrastructure as Code (IaC)** | Terraform |
| **Container Orchestration** | Kubernetes (Kind: 1 Control Plane, 2 Workers) |
| **CI/CD Strategy** | GitOps with ArgoCD |
| **Application** | Python Flask (FB Automation Tool) |
| **Networking** | Nginx Reverse Proxy + Manual Port Forwarding |
| **Observability** | Prometheus & Grafana (Helm Chart) |

## 🛠️ Step 1: Infrastructure Provisioning (Terraform)

```bash
cd terraform
terraform init
terraform apply --auto-approve
```

```bash
ssh -i "your-key.pem" ubuntu@<EC2_PUBLIC_IP>
```

## ☸️ Step 2: Kubernetes Cluster Setup (Kind)

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
```

```bash
kind create cluster --config kind-config.yaml --name fb-project
kubectl get nodes
```

## 🐙 Step 3: GitOps Setup (ArgoCD)

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl patch svc argocd-server -n argocd -p '{"spec": {"type": "NodePort"}}'
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo
```

## 📊 Step 4: Monitoring (Prometheus & Grafana)

```bash
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kind-prometheus prometheus-community/kube-prometheus-stack -n monitoring --set grafana.adminPassword=admin
```

## 🌐 Step 5: Networking & Access Strategy

### Nginx Reverse Proxy

```nginx
server {
    listen 80;
    server_name _;
    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:30007;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Port Forward Tunnels

```bash
sudo killall kubectl
nohup kubectl port-forward deployment/fb-app 30007:5000 --address 0.0.0.0 > /dev/null 2>&1 &
nohup kubectl port-forward svc/argocd-server -n argocd 8080:443 --address 0.0.0.0 > /dev/null 2>&1 &
nohup kubectl port-forward svc/kind-prometheus-grafana -n monitoring 31000:80 --address 0.0.0.0 > /dev/null 2>&1 &
nohup kubectl port-forward svc/kubernetes-dashboard -n kubernetes-dashboard 8081:443 --address 0.0.0.0 > /dev/null 2>&1 &
```

## 🔗 Access Points

| Service | URL | Credentials |
|--------|-----|-------------|
| Web App | http://<EC2_PUBLIC_IP> | N/A |
| ArgoCD | https://<EC2_PUBLIC_IP>:8080 | admin |
| Grafana | http://<EC2_PUBLIC_IP>:31000 | admin / admin |
| Kubernetes Dashboard | https://<EC2_PUBLIC_IP>:8081 | Token Required |

## 👨‍💻 Author

**Shubham Gote**  
DevOps Engineer | Cloud & Kubernetes Enthusiast
