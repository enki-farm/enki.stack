import json
import math
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request


class EmbeddingServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen([
            "vllm", "serve", "BAAI/bge-small-en-v1.5",
            "--revision", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
            "--served-model-name", "embedding", "--runner", "pooling",
            "--dtype", "float32", "--port", "8080",
            "--max-model-len", "512", "--max-num-seqs", "16",
            "--kv-cache-memory-bytes", "0",
            "--enforce-eager",
        ])
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                if cls.server.poll() is not None:
                    raise RuntimeError("vLLM exited before becoming ready")
                try:
                    urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5).close()
                    return
                except (urllib.error.URLError, TimeoutError):
                    threading.Event().wait(1)
            raise RuntimeError("vLLM did not become ready within 300 seconds")
        except BaseException:
            cls.stop_server()
            raise

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()

    def embed(self, inputs):
        request = urllib.request.Request(
            "http://127.0.0.1:8080/v1/embeddings",
            data=json.dumps({"model": "embedding", "input": inputs, "encoding_format": "float"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)["data"]

    def test_vectors_batch_order_normalization_and_similarity(self):
        texts = [
            "Represent this sentence for searching relevant passages: What is the capital of France?",
            "Paris is the capital city of France.",
            "A database transaction uses locks to maintain consistency.",
        ]
        results = self.embed(texts)
        self.assertEqual([result["index"] for result in results], [0, 1, 2])
        vectors = [result["embedding"] for result in results]
        for vector in vectors:
            self.assertEqual(len(vector), 384)
            self.assertTrue(all(math.isfinite(value) for value in vector))
            self.assertAlmostEqual(sum(value * value for value in vector), 1.0, places=3)
        relevant = sum(first * second for first, second in zip(vectors[0], vectors[1]))
        unrelated = sum(first * second for first, second in zip(vectors[0], vectors[2]))
        self.assertGreater(relevant, unrelated)
        scalar = self.embed(texts[1])[0]["embedding"]
        self.assertLess(max(abs(first - second) for first, second in zip(scalar, vectors[1])), 0.001)

    def test_oversized_input_not_silently_truncated(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.embed("hello " * 600)
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()