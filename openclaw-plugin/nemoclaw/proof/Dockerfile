# Zero-egress proof image: engram-lite + its embedding model fully baked,
# so the runtime container can be started with `--network none` and still
# run the complete memory loop. Build-time network is expected (that is the
# point — all downloads happen at bake time, exactly like the NemoClaw
# sandbox image).
FROM python:3.12-slim

# engine from the local dev tree (run.sh stages it into the build context)
COPY engine-src/ /src/
RUN pip install --no-cache-dir /src

# bake the embedding model into the image at a pinned path
ENV ENGRAM_MODEL_CACHE=/opt/engram/models
RUN python -c "from engram.embeddings.local import LocalEmbedder; \
    e = LocalEmbedder(); print(f'model baked: {e.model_name} (dim {e.dim})')"

COPY proof.py /proof.py
ENTRYPOINT ["python", "/proof.py"]
