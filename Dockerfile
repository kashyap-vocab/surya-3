# We must use the base server image, NOT the cuda image, to ensure it runs cleanly on CPU without NVIDIA drivers.
FROM ghcr.io/ggerganov/llama.cpp:server-b4202

EXPOSE 8002

ENTRYPOINT []

CMD sh -c '\
    if [ -x /server ]; then \
      BIN=/server; \
    elif [ -x /llama-server ]; then \
      BIN=/llama-server; \
    elif [ -x /app/llama-server ]; then \
      BIN=/app/llama-server; \
    else \
      BIN=llama-server; \
    fi; \
    echo "Using binary: $BIN"; \
    exec $BIN \
      --hf-repo bartowski/Qwen2.5-7B-Instruct-GGUF \
      --hf-file Qwen2.5-7B-Instruct-Q4_K_M.gguf \
      --host 0.0.0.0 \
      --port 8002 \
      --n-gpu-layers ${N_GPU_LAYERS:-0} \
      ${FLASH_ATTN} \
      --ctx-size 8192 \
      --parallel 4 \
      --batch-size 8192 \
      --ubatch-size 2048 \
      --threads 16 \
      --cont-batching \
'