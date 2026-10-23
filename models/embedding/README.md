# Embeddings


## Image embedding (cohere style)

Models like siglip need cohere style requests for image embeddings. So make sure that cohere compatible route is deployed. This bypasses gateway. Check if really best.

```json
{
  "inputs": [
    {
      "content": [
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,{{img_base64}}"
          }
        }
      ]
    }
  ],
  "model": "siglip2"
}
```