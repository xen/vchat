from sentence_transformers import SentenceTransformer
import sys
import os

# Для чистоты теста отключаем MPS/Metal, если torch его подхватывает
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"

if __name__ == "__main__":
    try:
        model = SentenceTransformer("data/models/giga-embeddings-instruct", device='cpu', trust_remote_code=True)
        print("Model loaded successfully on CPU.")
        emb = model.encode(["test string"], device='cpu')
        print("Embedding shape:", emb.shape)
        print("Embedding (first 5 values):", emb[0][:5])
        sys.exit(0)
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
