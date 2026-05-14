FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    cmake \
    curl \
    python3 \
    python3-pip \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN git clone https://github.com/ggerganov/llama.cpp.git

WORKDIR /opt/llama.cpp

RUN cmake -B build \
    -DGGML_CUDA=ON \
    -DBUILD_SHARED_LIBS=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_OPENSSL=ON \
    && cmake --build build --target llama-server -j$(nproc)

EXPOSE 7000

ENTRYPOINT []

CMD ["./build/bin/llama-server"]

CMD ./build/bin/llama-server \
      -hf ${MODEL} \
      --host 0.0.0.0 \
      --port 7000 \
      --n-gpu-layers 999 \
      --flash-attn on \
      --ctx-size 8192