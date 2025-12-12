# AI LiveMonitor - Realtime Meeting Assistant

リアルタイム会議アシスタントアプリケーション。音声をリアルタイムで文字起こしし、AIがミーティング中のアドバイスを提供します。

## システムアーキテクチャ

```
┌─────────────────┐     WebSocket      ┌─────────────────┐
│                 │  (Binary Audio)    │                 │
│    Frontend     │ ─────────────────► │    Backend      │
│   (Next.js)     │ ◄───────────────── │   (FastAPI)     │
│                 │   (JSON Messages)  │                 │
└─────────────────┘                    └─────────────────┘
        │                                      │
        │                                      │
        ▼                                      ▼
┌─────────────────┐                    ┌─────────────────┐
│   AudioWorklet  │                    │  Google Cloud   │
│  (PCM 16kHz)    │                    │  Speech-to-Text │
└─────────────────┘                    └─────────────────┘
                                               │
                                               ▼
                                       ┌─────────────────┐
                                       │   Vertex AI     │
                                       │    Gemini       │
                                       └─────────────────┘
```

## Google Cloud Platform 構成図

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                              Google Cloud Platform                                │
│                              Project: ailivemonitor                               │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────────┐ │
│  │                         Cloud Run (asia-northeast1)                          │ │
│  │  ┌───────────────────────────────────────────────────────────────────────┐  │ │
│  │  │                    Service: ailivemonitor                              │  │ │
│  │  │  ┌─────────────────────────────────────────────────────────────────┐  │  │ │
│  │  │  │                     Container (FastAPI)                          │  │  │ │
│  │  │  │                                                                  │  │  │ │
│  │  │  │   /ws (WebSocket)  ──────►  STT Thread  ──────►  Advice Task    │  │  │ │
│  │  │  │   /health (HTTP)            (gRPC)              (REST)          │  │  │ │
│  │  │  │                                                                  │  │  │ │
│  │  │  └─────────────────────────────────────────────────────────────────┘  │  │ │
│  │  │                              │                          │              │  │ │
│  │  └──────────────────────────────┼──────────────────────────┼──────────────┘  │ │
│  └─────────────────────────────────┼──────────────────────────┼─────────────────┘ │
│                                    │                          │                   │
│                                    ▼                          ▼                   │
│  ┌─────────────────────────────────────────┐  ┌─────────────────────────────────┐ │
│  │      Cloud Speech-to-Text API           │  │         Vertex AI API           │ │
│  │      (speech.googleapis.com)            │  │  (aiplatform.googleapis.com)    │ │
│  ├─────────────────────────────────────────┤  ├─────────────────────────────────┤ │
│  │                                         │  │                                 │ │
│  │  ┌───────────────────────────────────┐  │  │  ┌───────────────────────────┐  │ │
│  │  │   Streaming Recognition API       │  │  │  │   Gemini 2.0 Flash        │  │ │
│  │  │                                   │  │  │  │                           │  │ │
│  │  │   - 双方向 gRPC ストリーミング    │  │  │  │   - streamGenerateContent │  │ │
│  │  │   - リアルタイム音声認識          │  │  │  │   - リアルタイム生成       │  │ │
│  │  │   - 中間結果 + 確定結果           │  │  │  │   - 会議アドバイス生成     │  │ │
│  │  │                                   │  │  │  │                           │  │ │
│  │  └───────────────────────────────────┘  │  │  └───────────────────────────┘  │ │
│  │                                         │  │                                 │ │
│  │  入力: PCM 16kHz LINEAR16               │  │  入力: 文字起こしテキスト       │ │
│  │  出力: 認識テキスト (partial/final)     │  │  出力: アドバイステキスト       │ │
│  │                                         │  │                                 │ │
│  └─────────────────────────────────────────┘  └─────────────────────────────────┘ │
│                                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────────┐ │
│  │                              CI/CD Pipeline                                  │ │
│  │                                                                              │ │
│  │   GitHub Repository                Cloud Build              Container        │ │
│  │   (FORIFOR/AI_LiveMonitor)  ────►  Trigger      ────►      Registry (GCR)   │ │
│  │                                    (Dockerfile)             gcr.io/...       │ │
│  │                                                                    │         │ │
│  │                                                                    ▼         │ │
│  │                                                             Cloud Run        │ │
│  │                                                             (Deploy)         │ │
│  └─────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────────┐ │
│  │                              IAM & Security                                  │ │
│  │                                                                              │ │
│  │   Service Account: realtime-assistant@ailivemonitor.iam.gserviceaccount.com │ │
│  │                                                                              │ │
│  │   Roles:                                                                     │ │
│  │   - roles/speech.client        (Speech-to-Text API アクセス)                 │ │
│  │   - roles/aiplatform.user      (Vertex AI API アクセス)                      │ │
│  │   - roles/run.invoker          (Cloud Run 呼び出し)                          │ │
│  │                                                                              │ │
│  └─────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                   │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## 通信フロー詳細図

```
┌──────────┐                    ┌──────────┐                    ┌──────────────────┐
│  Browser │                    │ Cloud Run│                    │   GCP Services   │
│ (Client) │                    │ (Server) │                    │                  │
└────┬─────┘                    └────┬─────┘                    └────────┬─────────┘
     │                               │                                   │
     │  1. WebSocket Connect         │                                   │
     │  wss://xxx.run.app/ws         │                                   │
     │ ─────────────────────────────►│                                   │
     │                               │                                   │
     │  2. Connection Accepted       │                                   │
     │ ◄─────────────────────────────│                                   │
     │                               │                                   │
     │  3. Start Session             │                                   │
     │  {"type":"start","lang":"ja"} │                                   │
     │ ─────────────────────────────►│                                   │
     │                               │  4. Initialize STT Stream         │
     │                               │  (gRPC Bidirectional)             │
     │                               │ ─────────────────────────────────►│
     │                               │                                   │
     │  5. {"type":"started"}        │                                   │
     │ ◄─────────────────────────────│                                   │
     │                               │                                   │
     │  6. Audio Chunk (Binary)      │                                   │
     │  [PCM 16kHz Int16 data]       │                                   │
     │ ─────────────────────────────►│  7. Forward Audio                 │
     │                               │  StreamingRecognizeRequest        │
     │                               │ ─────────────────────────────────►│
     │                               │                                   │
     │                               │  8. STT Response                  │
     │                               │  (partial/final transcript)       │
     │                               │ ◄─────────────────────────────────│
     │  9. STT Result                │                                   │
     │  {"type":"stt.partial/final"} │                                   │
     │ ◄─────────────────────────────│                                   │
     │                               │                                   │
     │                               │  10. Generate Advice (every 3s)   │
     │                               │  Gemini streamGenerateContent     │
     │                               │ ─────────────────────────────────►│
     │                               │                                   │
     │                               │  11. Advice Stream Response       │
     │                               │ ◄─────────────────────────────────│
     │  12. Advice Delta             │                                   │
     │  {"type":"advice.delta"}      │                                   │
     │ ◄─────────────────────────────│                                   │
     │                               │                                   │
     │  ... (repeat 6-12) ...        │                                   │
     │                               │                                   │
     │  13. Stop Session             │                                   │
     │  {"type":"stop"}              │                                   │
     │ ─────────────────────────────►│  14. Close STT Stream             │
     │                               │ ─────────────────────────────────►│
     │                               │                                   │
     │  15. WebSocket Close          │                                   │
     │ ◄─────────────────────────────│                                   │
     │                               │                                   │
     ▼                               ▼                                   ▼
```

## 技術スタック

### フロントエンド
- **フレームワーク**: Next.js 15 (App Router)
- **言語**: TypeScript
- **スタイリング**: Tailwind CSS
- **音声処理**: Web Audio API + AudioWorklet
- **通信**: WebSocket (Binary + JSON)

### バックエンド
- **フレームワーク**: FastAPI
- **言語**: Python 3.11+
- **パッケージ管理**: Poetry
- **非同期処理**: asyncio + threading
- **通信**: WebSocket

### クラウドサービス (Google Cloud Platform)

#### 1. Cloud Speech-to-Text API
リアルタイム音声認識サービス。ストリーミング認識機能を使用。
- **エンドポイント**: `speech.googleapis.com`
- **認識モード**: Streaming Recognition (双方向gRPC)
- **対応言語**: 日本語 (ja-JP) をデフォルトで使用
- **機能**: 中間結果 (interim_results)、自動句読点 (enable_automatic_punctuation)

#### 2. Vertex AI (Gemini)
Google の最新 LLM サービス。会議アドバイス生成に使用。
- **モデル**: `gemini-2.0-flash` (高速推論モデル)
- **API**: Vertex AI Generative AI API
- **エンドポイント**: `{location}-aiplatform.googleapis.com`
- **機能**: ストリーミング生成 (streamGenerateContent)

#### 3. Gemini 2.5 TTS (Text-to-Speech)
AIアドバイスを音声で読み上げる機能。
- **モデル**: `gemini-2.5-flash-preview-tts`
- **出力形式**: PCM 24kHz, 16-bit, モノラル
- **ボイス**: Kore (日本語対応)
- **機能**: テキストから自然な音声を生成

#### 4. Cloud Run
サーバーレスコンテナ実行環境。バックエンドAPIをホスト。
- **特徴**: 自動スケーリング、従量課金、WebSocket対応
- **コンテナランタイム**: Docker

#### 5. Cloud Build
CI/CD パイプライン。GitHubからの自動ビルド。
- **トリガー**: GitHub push イベント
- **ビルダー**: `gcr.io/cloud-builders/docker`

#### 6. Google Container Registry (GCR)
Dockerイメージの保存・管理。
- **形式**: `gcr.io/{project-id}/{image-name}:{tag}`

## データフロー

### 1. 音声キャプチャ (フロントエンド)

```typescript
// ブラウザのマイクから音声を取得
const stream = await navigator.mediaDevices.getUserMedia({
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
});

// AudioWorkletで音声をPCM 16kHzに変換
await audioCtx.audioWorklet.addModule("/pcm-worklet.js");
const node = new AudioWorkletNode(audioCtx, "pcm-processor");
```

### 2. AudioWorklet処理 (pcm-worklet.js)

```javascript
// Float32 → 16kHz リサンプリング → Int16 PCM変換
process(inputs) {
  const input = inputs[0][0];
  // 48kHz → 16kHz ダウンサンプリング (3:1)
  // Float32 [-1, 1] → Int16 [-32768, 32767]
  this.port.postMessage(int16Buffer.buffer);
}
```

### 3. WebSocket通信

**フロントエンド → バックエンド:**
- `{ type: "start", lang: "ja-JP" }` - セッション開始
- `ArrayBuffer` (Binary) - 音声データ (PCM 16kHz Int16)
- `{ type: "stop" }` - セッション終了

**バックエンド → フロントエンド:**
- `{ type: "started" }` - セッション開始確認
- `{ type: "stt.partial", text: "..." }` - 中間文字起こし結果
- `{ type: "stt.final", text: "..." }` - 確定文字起こし結果
- `{ type: "advice.delta", text: "..." }` - AIアドバイス (ストリーミング)
- `{ type: "advice.final" }` - AIアドバイス完了
- `{ type: "tts.start" }` - TTS音声生成開始
- `ArrayBuffer` (Binary) - TTS音声データ (PCM 24kHz Int16)
- `{ type: "tts.complete", format: "pcm", sample_rate: 24000 }` - TTS音声生成完了
- `{ type: "tts.error", message: "..." }` - TTSエラー
- `{ type: "error", message: "..." }` - エラー通知

### 4. バックエンド処理

```python
# セッション状態管理
class SessionState:
    audio_q: Queue          # 音声チャンクキュー (maxsize=500)
    stt_result_q: asyncio.Queue  # STT結果キュー
    recent_text: Deque[str]      # 直近30件の文字起こし (circular buffer)
```

**スレッド構成:**
- **メインスレッド (asyncio)**: WebSocket処理、結果送信
- **STTスレッド (threading)**: Google Cloud Speech-to-Text gRPC ストリーミング
- **アドバイスタスク (asyncio)**: Gemini API呼び出し

### 5. Speech-to-Text (STT)

```python
# ストリーミング認識設定
config = speech.RecognitionConfig(
    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
    sample_rate_hertz=16000,
    language_code="ja-JP",
    enable_automatic_punctuation=True,
)
streaming_config = speech.StreamingRecognitionConfig(
    config=config,
    interim_results=True,      # 中間結果を有効化
    single_utterance=False,    # 連続認識
)
```

### 6. Gemini アドバイス生成

```python
# プロンプト構築
prompt = f"""あなたは会議中のリアルタイムアシスタントです。
以下の会話ログに対して、ユーザーが次に言うべき「1文」を最優先で出してください。
次に、根拠(箇条書き2〜3)、次に聞く質問(1〜2)を出してください。

[会話ログ]
{recent_transcript}
"""

# ストリーミング生成
stream = client.models.generate_content_stream(
    model="gemini-2.0-flash",
    contents=prompt,
    config=GenerateContentConfig(temperature=0.4, max_output_tokens=300),
)
```

## クラウド構成

### Cloud Run 設定

| 項目 | 値 |
|------|-----|
| リージョン | asia-northeast1 (東京) |
| CPU | 1 vCPU |
| メモリ | 512 MB |
| 最小インスタンス | 0 |
| 最大インスタンス | 10 |
| リクエストタイムアウト | 300秒 |
| コンテナポート | 8000 |

### 環境変数

| 変数名 | 説明 | 例 |
|--------|------|-----|
| `GCP_PROJECT` | GCPプロジェクトID | `ailivemonitor` |
| `GCP_LOCATION` | Vertex AIリージョン | `asia-northeast1` |
| `GEMINI_MODEL` | 使用するGeminiモデル | `gemini-2.0-flash` |
| `TTS_MODEL` | TTSモデル | `gemini-2.5-flash-preview-tts` |
| `TTS_VOICE` | TTSボイス名 | `Kore` |
| `TTS_ENABLED` | TTS機能の有効/無効 | `true` |
| `GOOGLE_AI_API_KEY` | Google AI API キー (TTS用、オプション) | `AIza...` |
| `GOOGLE_APPLICATION_CREDENTIALS` | サービスアカウントキーのパス | `/app/credentials.json` |

### 必要なGCP API

1. **Cloud Speech-to-Text API** - 音声認識
2. **Vertex AI API** - Gemini モデル
3. **Cloud Run API** - コンテナデプロイ
4. **Cloud Build API** - CI/CD

### サービスアカウント権限

- `roles/speech.client` - Speech-to-Text API アクセス
- `roles/aiplatform.user` - Vertex AI API アクセス

## ディレクトリ構造

```
realtime-assistant/
├── api/                          # バックエンド
│   ├── main.py                   # FastAPI アプリケーション
│   ├── pyproject.toml            # Poetry 依存関係
│   ├── Dockerfile                # コンテナビルド設定
│   └── credentials.json          # サービスアカウントキー (gitignore)
│
├── frontend/                     # フロントエンド
│   ├── src/
│   │   └── app/
│   │       └── page.tsx          # メインページコンポーネント
│   ├── public/
│   │   └── pcm-worklet.js        # AudioWorklet プロセッサ
│   ├── next.config.ts            # Next.js 設定
│   └── .env.local                # 環境変数
│
└── README.md                     # このファイル
```

## 効率性の最適化

### 1. asyncio.Queue によるスレッド間通信
ポーリングパターンを排除し、`call_soon_threadsafe()` でスレッドセーフな非同期キュー通信を実現。

### 2. Circular Buffer (deque)
直近の文字起こしを `deque(maxlen=30)` で管理し、メモリ効率を向上。

### 3. 音声キューの制限
`Queue(maxsize=500)` で音声チャンクの蓄積を制限し、メモリオーバーフローを防止。

### 4. ストリーミングレスポンス
STTとGeminiの両方でストリーミングを使用し、低レイテンシーを実現。

## ローカル開発

### バックエンド起動

```bash
cd api
poetry install
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
export GCP_PROJECT=your-project-id
export GCP_LOCATION=asia-northeast1
poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### フロントエンド起動

```bash
cd frontend
npm install
npm run dev
```

ブラウザで http://localhost:3000 にアクセス

## デプロイ

### Cloud Build トリガー設定

1. GitHubリポジトリを接続
2. トリガーを作成:
   - ブランチパターン: `^devin/.*` または `^main$`
   - Dockerfileのディレクトリ: `/api`
3. トリガーを実行してイメージをビルド

### Cloud Run デプロイ

1. Cloud Run サービスを作成
2. Container Registry から最新イメージを選択
3. 環境変数を設定
4. デプロイ

## ライセンス

MIT License
