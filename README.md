# Surya AI LLM Proxy & Backend

This repository contains a full stack application for running a secure, identity-masked Large Language Model (LLM) API. It uses `llama.cpp` for high-performance inference and a FastAPI-based wrapper to ensure the safety and anonymity of the internal model.

## Features

- **High-Performance Inference**: Uses `llama.cpp` to run quantized models (GGUF format) efficiently on either CPU or GPU.
- **Identity Masking**: The FastAPI wrapper hides the true identity of the model (e.g., Qwen, Llama, Mistral) and presents it purely as `surya-3`.
- **Probe Protection**: Automatically detects and blocks attempts by users to probe the model for its system prompt or internal model name.
- **Data Sanitization**: Strips OpenAI-related headers and scrubs sensitive keywords from the LLM's output.
- **Automated Logging**: All API requests and LLM responses are securely and automatically logged into daily rotating JSON files (`requests.log` and `errors.log`).
- **OpenAPI / Swagger UI**: Built-in Swagger documentation available at `/docs` to easily test and explore the API.
- **Code Obfuscation**: The wrapper Python code is compiled into a C-extension binary (`.so`) using Cython during the Docker build process, completely hiding the source code in the final runtime container.

## Application Architecture

1.  **Wrapper Service (`surya-api`)**:
    -   Runs on port `9000`.
    -   Intercepts user requests, applies system guardrails, blocks malicious probes, and forwards the cleaned request to the backend.
    -   Logs every interaction to the mounted `/logs` directory.
2.  **LLM Backend (`surya3-llm-backend`)**:
    -   Runs on port `8002` (internal only).
    -   Powered by `llama.cpp`. Currently configured to run `bartowski/Qwen2.5-7B-Instruct-GGUF`.
    -   Automatically downloads the model from Hugging Face on the first run.

## Prerequisites

- **Docker** and **Docker Compose** installed.
- (Optional but recommended) NVIDIA GPU with proper Docker/WSL configuration for hardware acceleration.
- A **Hugging Face Access Token** (Read permissions).

## Quick Start Guide

### 1. Configure Hugging Face Token

The backend needs to download the model weights from Hugging Face. You must provide a valid read token.

1.  Create an account at [huggingface.co](https://huggingface.co) and generate an Access Token (Settings -> Access Tokens).
2.  In the root of this project, create or open the `.env` file.
3.  Add your token:
    ```env
    HF_TOKEN=hf_your_actual_token_here
    ```

### 2. Build and Run the Application

Open your terminal in the project directory and run:

```bash
docker-compose up --build -d
```

### 3. Monitor the Boot Process

**Important**: Because the model file is several Gigabytes, it will take some time to download on the very first run.

Check the backend logs to monitor the download progress:

```bash
docker logs -f surya3-llm-backend
```

Wait until the download completes and you see a message similar to: `HTTP server listening, hostname: 0.0.0.0, port: 8002`. Press `Ctrl+C` to exit the log view.

### 4. Test the API via Swagger UI

Once the backend is fully loaded, open your web browser and navigate to:

👉 **http://localhost:9000/docs**

From the Swagger UI, you can test the `GET /v1/models` and `POST /v1/chat/completions` endpoints.

Alternatively, you can test it via PowerShell/cURL:

**PowerShell Example:**
```powershell
Invoke-RestMethod -Uri "http://localhost:9000/v1/chat/completions" `
  -Method Post `
  -Headers @{ "Content-Type" = "application/json" } `
  -Body '{"messages": [{"role": "user", "content": "Write a short haiku about coding"}], "stream": false}' | ConvertTo-Json -Depth 5
```

### 5. Check the Logs

After sending requests, you can view the detailed logs on your host machine.
Navigate to the `logs/` directory in your project root.
-   `requests.log` contains detailed JSON entries for every request, including token usage, input prompts, and output text.
-   `errors.log` contains blocked probe attempts and system errors.

## Switching Between CPU and GPU

The repository is configured to run on **CPU by default** for maximum compatibility. You can easily switch to GPU acceleration by editing the `.env` file.

### Running on CPU (Default)
In your `.env` file, ensure the values are set for CPU:
```env
# 0 means CPU only
N_GPU_LAYERS=0
# Empty means no flash attention
FLASH_ATTN=
```

### Running on GPU (For maximum speed)
1. Open your `.env` file and change the variables to enable GPU offloading and flash attention:
   ```env
   # Offload all layers to GPU
   N_GPU_LAYERS=999
   # Enable flash attention
   FLASH_ATTN="--flash-attn on"
   ```
2. Open `docker-compose.yml` and uncomment the `deploy` block under the `llm-backend` service so Docker allocates the GPU:
   ```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
   ```
3. Rebuild the containers to apply the changes:
   ```bash
   docker-compose up --build -d
   ```

## Running on Google Cloud Platform (GCP)

This application is fully containerized and cloud-ready. You can easily deploy it to GCP.

### Option A: Compute Engine (Recommended for GPU)
If you want to use GPUs for fast inference:
1.  Create a VM instance in Compute Engine with a GPU attached (e.g., NVIDIA L4 or T4).
2.  Ensure you select an OS image with Docker and NVIDIA drivers pre-installed (e.g., "Deep Learning VM").
3.  Clone this repository onto the VM.
4.  Configure the repository for GPU (see "Running on GPU" above).
5.  Run `docker-compose up --build -d`.
6.  Ensure your VPC Firewall allows inbound TCP traffic on port `9000`.

### Option B: Cloud Run (CPU Only)
If you do not need a GPU and want a fully managed environment:
1.  Build and push the two container images to Google Artifact Registry using Google Cloud Build.
2.  Deploy the `llm-backend` image as a Cloud Run service (ensure it is strictly internal and allocate sufficient memory, e.g., 8GB+).
3.  Deploy the `wrapper` image as a public Cloud Run service, setting the `INTERNAL_LLM_URL` environment variable to the internal URL of the backend service.

## Configuration & Customization

You can customize the application behavior by modifying the environment variables in `docker-compose.yml`:

-   `INTERNAL_MODEL`: The actual model name you are running (e.g., `Qwen2.5-7B-Instruct-Q4_K_M.gguf`).
-   `PUBLIC_MODEL_NAME`: The name the wrapper will present to users (e.g., `surya-3`).
-   `BACKEND_IDENTITY`: The identity the system guard will claim if probed (e.g., `Surya AI`).

To change the model entirely, update the `CMD` arguments in the `Dockerfile` to point to a different Hugging Face repository and GGUF file.