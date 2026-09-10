# Feedback and Improvement Roadmap

Last updated: 2026-09-10

## Overview

This document summarizes feedback received during the project review and the improvement roadmap for the Kubernetes-based Data Center AI Monitoring System.

The current system successfully provides infrastructure deployment, anomaly detection, Prometheus metrics, Grafana visualization, Kubernetes self-healing, NetworkPolicy, HPA, and K3s recovery verification.

The next phase focuses on improving observability and providing actionable AI-based analysis rather than only displaying infrastructure metrics.

## Feedback

### 1. Limited Data Collection

The current monitoring system mainly collects numerical infrastructure metrics such as:

* CPU usage
* Memory usage
* Temperature
* Power consumption
* Prediction count
* Anomaly count

This is not sufficient for diagnosing operational problems in detail.

### 2. Need Faster Problem Identification

The current Grafana dashboard allows users to observe system status, but users still need to manually inspect dashboards to identify problems.

The system should detect meaningful changes and surface problems immediately.

### 3. LLM Analysis Function Is Not Yet Integrated

An EXAONE LLM model has been prepared, but it is not yet connected to the monitoring workflow.

The goal is to allow users to request analyses such as:

> Analyze recent anomalies in the data center.

The backend should retrieve relevant monitoring data from AWS RDS MySQL, summarize the structured data, and provide it to the LLM for natural-language analysis.

The LLM should not directly execute arbitrary SQL queries against the database.

### 4. Need Support for Unstructured Data

The current system mainly handles structured numerical data.

Operational analysis should also consider unstructured data such as:

* Application logs
* Error logs
* Kubernetes Events
* Infrastructure warning messages

Combining structured metrics with unstructured logs can provide better incident analysis.

---

# Improvement Roadmap

## Priority 1 — EXAONE + RDS Analysis

Status: Planned

Implement an AI analysis API using the locally stored EXAONE model.

Target flow:

```text
User Question
      |
      v
FastAPI Analysis Endpoint
      |
      v
AWS RDS Query / Aggregation
      |
      v
Structured Monitoring Context
      |
      v
EXAONE LLM
      |
      v
Natural Language Analysis
```

Example questions:

```text
Analyze recent anomalies.

Which server currently appears most unstable?

What happened to server02 during the last anomaly?

Summarize the recent data center status.
```

## Priority 2 — Expand Observability Metrics

Status: Planned

Add additional operational metrics.

Application metrics:

```text
HTTP request count
HTTP error rate
API response latency
Prediction latency
Database query latency
Database connection failures
```

Kubernetes metrics:

```text
Pod restart count
Pod status
Deployment replica count
HPA replica changes
```

Node metrics:

```text
Disk utilization
Disk I/O
Network traffic
CPU utilization
Memory utilization
```

Existing Prometheus components such as Node Exporter and kube-state-metrics should be reused where possible.

## Priority 3 — Alerting

Status: Planned

Introduce proactive incident detection using Grafana Alerting or Prometheus Alertmanager.

Candidate alerts:

```text
High CPU usage
High temperature
High anomaly rate
API unavailable
Pod restart
High API error rate
High response latency
```

Target flow:

```text
Metric Change
     |
     v
Threshold / Rule Detection
     |
     v
Alert Firing
     |
     v
Operator Investigation
     |
     v
Alert Resolved
```

The alert workflow should be tested by intentionally generating load or anomaly conditions.

## Priority 4 — Unstructured Log Collection

Status: Planned

Collect and analyze operational logs in addition to metrics.

Initial log sources:

```text
FastAPI application logs
Simulator logs
Kubernetes Events
Application error messages
```

Example events:

```text
CPU overload detected
Cooling system warning
Database connection timeout
API request failed
Pod restarted
```

A lightweight implementation should be preferred before introducing a full ELK/OpenSearch stack.

## Priority 5 — Metrics + Logs Incident Analysis

Status: Planned

Combine structured monitoring data and unstructured logs into an incident context.

Example:

```text
server02

CPU                 95%
Temperature         92 C
Prediction           ANOMALY
Probability          1.0
Log                  cooling system warning
```

Expected LLM output:

```text
server02 experienced simultaneous increases in CPU utilization
and temperature. A cooling-related warning was also detected.

The most likely cause is a cooling failure or thermal overload.
```

This will evolve the project from anomaly detection toward a lightweight AIOps-style monitoring system.

## Priority 6 — Grafana Dashboard Improvements

Status: Planned

Improve the dashboard from basic metric visualization to operational monitoring.

Candidate dashboard sections:

```text
System Overview
Infrastructure
Application
AI Anomaly Detection
Kubernetes
Incidents
```

Candidate panels:

```text
Current anomaly count
Anomaly rate
API error rate
API latency
Pod restart count
HPA replica count
Node CPU / Memory / Disk
Recent incidents
```

## Priority 7 — User-facing AI Analysis Interface

Status: Planned

Provide a simple interface for users to request AI-based monitoring analysis.

Possible implementation:

```text
Simple Web UI / Gradio
        |
        v
FastAPI
        |
        +--> RDS monitoring data
        |
        +--> Logs
        |
        v
EXAONE
        |
        v
Analysis Result
```

Grafana will remain responsible for visualization, while the AI interface will focus on explanation and incident analysis.

## Priority 8 — High Availability Design

Status: Planned

The current system uses one EC2 instance with a single-node K3s cluster.

Therefore, the current environment is not infrastructure-level High Availability.

Future architecture can consider:

```text
Load Balancer
      |
      v
Multi-node Kubernetes / K3s
 ├── Node 1
 ├── Node 2
 └── Node 3
      |
      v
AWS RDS Multi-AZ
```

Additional considerations:

```text
AWS ALB / NLB
RDS Multi-AZ
Shared persistent storage such as EFS
Pod Anti-Affinity
PodDisruptionBudget
Multiple replicas
Multi-AZ deployment
```

For this mini project, documenting the architecture and limitations is more appropriate than building the entire HA environment.

---

## Target Project Direction

The final goal is to evolve the project from:

```text
Metrics
  |
  v
Anomaly Detection
  |
  v
Grafana Visualization
```

into:

```text
Metrics + Logs + Kubernetes Events
              |
              v
       Anomaly Detection
              |
              v
        Alert / Incident
              |
              v
        Context Aggregation
              |
              v
          EXAONE LLM
              |
              v
   User-readable Root Cause Analysis
```

This provides a clearer operational workflow:

**Collect → Detect → Alert → Investigate → Explain**

