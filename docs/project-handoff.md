# Project Handoff

Last updated: 2026-09-10

## Project

Kubernetes-based Data Center AI Monitoring System

GitHub repository:

```text
Shinsangseok/datacenter-project
```

Branch:

```text
main
```

## Current Architecture

```text
Data Center Simulator
        |
        | HTTP
        v
FastAPI Inference Server
        |
        +--> RandomForest Anomaly Detection
        |
        +--> AWS RDS MySQL
        |
        +--> Prometheus Metrics
                     |
                     v
                  Grafana
```

Current infrastructure:

```text
AWS EC2
└── Ubuntu 24.04
    └── K3s single-node Kubernetes
        ├── datacenter-app
        │   └── FastAPI / RandomForest
        ├── datacenter-sim
        │   └── Data Center Simulator
        └── monitoring
            ├── Prometheus
            ├── Grafana
            └── Grafana Image Renderer

AWS RDS MySQL
└── measurements
```

## Kubernetes Environment

K3s version:

```text
v1.36.4+k3s1
```

Node:

```text
ip-172-31-54-143
Role: control-plane
Single node
```

Namespaces:

```text
datacenter-app
datacenter-sim
monitoring
kube-system
```

KUBECONFIG is configured in `~/.bashrc`:

```text
export KUBECONFIG=$HOME/.kube/config
```

The Ubuntu user can therefore run `kubectl` commands without sudo.

## Application

FastAPI Deployment:

```text
Namespace: datacenter-app
Deployment: datacenter-api
Default replicas: 2
Pod label: app=datacenter-api
```

Resources per API Pod:

```text
CPU request: 100m
CPU limit: 1
Memory request: 256Mi
Memory limit: 1Gi
```

Service:

```text
Name: datacenter-api
Type: LoadBalancer
Service port: 8080
Target port: 8000
```

Internal Simulator endpoint:

```text
http://datacenter-api.datacenter-app.svc.cluster.local:8080
```

## Data Center Simulator

Deployment:

```text
Namespace: datacenter-sim
Deployment: datacenter-simulator
Replica: 1
Pod label: app=datacenter-simulator
```

Configuration:

```text
SERVER_COUNT=6
INTERVAL_SECONDS=10
```

The simulator continuously generates virtual data-center measurements and sends them to the FastAPI inference server.

Example scenarios:

```text
normal
overload
cooling_fault
```

## AI Anomaly Detection

Model:

```text
RandomForestClassifier
```

Prediction classes:

```text
NORMAL
ANOMALY
```

Measurement and prediction results are stored in AWS RDS MySQL.

Table:

```text
measurements
```

Columns:

```text
measured_at
server_id
cpu
memory
temperature
power
prediction
probability
```

## EXAONE LLM

An EXAONE model has already been downloaded and stored.

The model is not yet integrated into the running monitoring application.

Target architecture:

```text
User Question
      |
      v
FastAPI Analysis API
      |
      v
RDS Query / Aggregation
      |
      v
Monitoring Context
      |
      v
EXAONE LLM
      |
      v
Natural Language Analysis
```

Important design decision:

The LLM should not directly execute arbitrary SQL against AWS RDS.

The backend should retrieve and aggregate controlled monitoring data before passing the context to EXAONE.

TODO before implementation:

```text
Confirm EXAONE model name
Confirm model version
Confirm exact model path
Confirm model size
Check required Python packages
Check EC2 available RAM and disk
```

## AWS RDS MySQL

FastAPI connects to AWS RDS using:

```text
DB_HOST
DB_PORT
DB_NAME
DB_USER
DB_PASSWORD
```

SSL CA:

```text
/home/ubuntu/rds-certs/global-bundle.pem
```

Never commit database passwords or credentials to GitHub.

## Prometheus

Helm release:

```text
prometheus
```

Chart:

```text
prometheus-29.27.2
```

Prometheus version:

```text
v3.14.0
```

Current custom application metrics:

```text
datacenter_cpu_percent
datacenter_memory_percent
datacenter_temperature_celsius
datacenter_power_watts
datacenter_predictions_total
datacenter_anomalies_total
```

Additional Kubernetes and infrastructure metrics are available through:

```text
node-exporter
kube-state-metrics
metrics-server
```

## Grafana

Grafana Chart:

```text
grafana-community/grafana 13.2.2
```

Grafana App version:

```text
13.2.1
```

Grafana Image Renderer is enabled.

Prometheus datasource:

```text
http://prometheus-server.monitoring.svc.cluster.local
```

Dashboard:

```text
Data Center AI Monitoring Dashboard
```

Current panels:

```text
Server CPU Usage
Server Memory Usage
Server Temperature
Server Power Usage
Prediction Status
Anomaly Rate
```

Saved files:

```text
grafana/dashboards/datacenter-monitoring-dashboard.json
grafana/screenshots/datacenter-monitoring-dashboard.png
```

## Local Monitoring Access

Prometheus and Grafana use systemd-managed kubectl port-forward services.

```text
prometheus-portforward.service
grafana-portforward.service
```

EC2:

```text
127.0.0.1:9090 -> Prometheus
127.0.0.1:3000 -> Grafana
```

Windows SSH tunnel:

```text
localhost:19090 -> EC2:9090 -> Prometheus
localhost:13000 -> EC2:3000 -> Grafana
```

## Kubernetes Self-Healing

Self-healing was verified by manually deleting one FastAPI Pod.

Observed sequence:

```text
Running
   |
   v
Terminating
   |
   v
Deleted
   |
   v
Pending
   |
   v
ContainerCreating
   |
   v
Running
```

The `datacenter-api` Deployment automatically restored its desired replica count of two Pods.

## NetworkPolicy

File:

```text
k8s/simulator-networkpolicy.yaml
```

Policy:

```text
simulator-egress-policy
```

The Simulator is allowed to communicate with:

```text
Kubernetes DNS
TCP/UDP 53

datacenter-api Pods
TCP 8000
```

Important:

The Kubernetes Service listens on port 8080 but forwards requests to FastAPI Pods on target port 8000.

NetworkPolicy verification:

```text
Simulator -> API with policy
HTTP 200

Simulator -> Prometheus with policy
Blocked

Simulator -> Prometheus without policy
HTTP 200
```

## Horizontal Pod Autoscaler

File:

```text
k8s/hpa.yaml
```

HPA:

```text
datacenter-api-hpa
```

Configuration:

```text
minReplicas: 2
maxReplicas: 3
target CPU utilization: 20%
```

CPU request per API Pod:

```text
100m
```

Verification:

```text
Normal state
2 Pods

CPU load generated
2 -> 3 Pods

Load removed
3 -> 2 Pods
```

Peak CPU utilization observed during the test was approximately 334%.

Both Scale-Out and Scale-In were successfully verified.

## K3s Recovery Verification

The K3s service was manually restarted:

```text
sudo systemctl restart k3s
```

After restart the following were verified:

```text
K3s service: active
Kubernetes node: Ready

FastAPI: Running
Simulator: Running
Prometheus: Running
Grafana: Running
Grafana Image Renderer: Running

Simulator -> FastAPI: HTTP 200

HPA metrics: operational
HPA replicas: 2

prometheus-portforward: active
grafana-portforward: active

Simulator prediction generation: operational
FastAPI -> AWS RDS storage: operational
```

This verifies service-level recovery after restarting K3s.

The environment is still not infrastructure-level High Availability because Kubernetes runs on one EC2 instance.

## Current Important Files

```text
README.md

docs/
├── feedback-and-roadmap.md
└── project-handoff.md

backend/
└── main.py

k8s/
├── configmap.yaml
├── deployment.yaml
├── grafana-values.yaml
├── hpa.yaml
├── namespaces.yaml
├── prometheus-values.yaml
├── service.yaml
├── simulator-configmap.yaml
├── simulator-deployment.yaml
└── simulator-networkpolicy.yaml

grafana/
├── dashboards/
│   └── datacenter-monitoring-dashboard.json
└── screenshots/
    └── datacenter-monitoring-dashboard.png
```

## Completed Work

```text
AWS EC2 / K3s environment
FastAPI inference API
RandomForest anomaly detection
Data Center Simulator
AWS RDS persistence
Prometheus metrics
Grafana dashboard
Grafana Image Renderer
Dashboard JSON / PNG export
Kubernetes Self-Healing verification
NetworkPolicy implementation and verification
Horizontal Pod Autoscaler
HPA Scale-Out / Scale-In verification
K3s restart recovery verification
GitHub documentation
```

## Current Limitations

```text
Monitoring data is still mainly numerical metrics.

Application-level observability is limited.

There is no proactive alerting workflow.

Unstructured logs are not centrally collected.

EXAONE is downloaded but not integrated.

There is no user-facing LLM analysis interface.

Metrics and logs are not correlated into incidents.

Kubernetes runs on a single EC2 node.

The current architecture is not High Availability.
```

## Next Development Priorities

Detailed roadmap:

```text
docs/feedback-and-roadmap.md
```

Recommended implementation order:

```text
1. EXAONE + RDS Analysis
2. Expand Observability Metrics
3. Alerting
4. Unstructured Log Collection
5. Metrics + Logs Incident Analysis
6. Grafana Dashboard Improvements
7. User-facing AI Analysis Interface
8. High Availability Design
```

## Next Session - Start Here

Before making changes:

```bash
cd ~/datacenter-project

git status
kubectl get nodes
kubectl get pods -A
kubectl get hpa -A
kubectl get networkpolicy -A
```

Then review:

```text
docs/project-handoff.md
docs/feedback-and-roadmap.md
```

The next implementation task should start with EXAONE + RDS analysis.

Before coding, verify:

```text
EXAONE model name
EXAONE model version
Exact model storage path
Model size
Required Python packages
Available EC2 RAM
Available EC2 disk
```

Then design the RDS-to-LLM analysis flow before modifying the running FastAPI application.

## Security Notes

Never commit:

```text
DB_PASSWORD
Database credentials
Kubernetes Secret values
Grafana administrator password
AWS credentials
SSH private key / PEM file
API tokens
```

Only non-sensitive configuration and architecture information should be stored in GitHub.

