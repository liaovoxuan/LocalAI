import { common } from '@kit.AbilityKit';
import { buffer } from '@kit.ArkTS';
import { fileIo as fs } from '@kit.CoreFileKit';
import { http } from '@kit.NetworkKit';
import nativeCloudAI from 'libcloudai_harmony.so';

interface NativeRequest {
  url: string;
  body: string;
  authorization: string;
}

interface ChatMessage {
  role: string;
  content: string;
}

@Entry
@Component
struct Index {
  @State private prompt: string = '';
  @State private apiKey: string = '';
  @State private provider: string = 'openai_official';
  @State private baseUrl: string = 'https://api.openai.com/v1';
  @State private model: string = 'gpt-4.1-mini';
  @State private status: string = 'Loading native CloudAI core...';
  @State private busy: boolean = false;
  @State private agentMode: boolean = false;
  @State private messages: ChatMessage[] = [];
  private appDataDir: string = '';

  aboutToAppear(): void {
    const context = getContext(this) as common.UIAbilityContext;
    this.appDataDir = context.filesDir;
    try {
      const nativeStatus = JSON.parse(nativeCloudAI.getStatus(this.appDataDir)) as Record<string, string>;
      this.provider = nativeStatus.provider || this.provider;
      this.model = nativeStatus.model || this.model;
      this.baseUrl = nativeStatus.base_url || this.providerDefaults(this.provider) || this.baseUrl;
      this.status = `${nativeStatus.provider} / ${nativeStatus.model}`;
    } catch (error) {
      this.status = `Native core unavailable: ${error}`;
    }
  }

  private providerDefaults(code: string): string {
    switch (code) {
      case 'openai_official': return 'https://api.openai.com/v1';
      case 'openrouter': return 'https://openrouter.ai/api/v1';
      case 'deepseek': return 'https://api.deepseek.com/v1';
      case 'siliconflow': return 'https://api.siliconflow.cn/v1';
      case 'openai_compatible': return 'http://localhost:8000/v1';
      default: return '';
    }
  }

  private providerIndex(): number {
    const codes: string[] = ['openai_official', 'openai_compatible', 'openrouter', 'deepseek', 'siliconflow', 'custom'];
    const index = codes.indexOf(this.provider);
    return index >= 0 ? index : codes.length - 1;
  }

  private async saveProvider(): Promise<void> {
    const endpoint = this.baseUrl.trim();
    const parsed = endpoint.match(/^https?:\/\/[^\s/]+(?:[/?#].*)?$/i);
    if (!parsed || !this.model.trim()) {
      this.status = 'Provider URL and model are required.';
      return;
    }
    try {
      nativeCloudAI.configureProvider(this.appDataDir, this.provider, this.baseUrl.trim(), this.model.trim());
      if (this.apiKey.trim()) nativeCloudAI.setApiKey(this.appDataDir, this.provider, this.apiKey.trim());
      this.apiKey = '';
      this.status = 'Provider settings and API key saved.';
    } catch (error) {
      this.status = `API key save failed: ${error}`;
    }
  }

  private async send(): Promise<void> {
    const text = this.prompt.trim();
    if (!text || this.busy) return;
    this.prompt = '';
    this.messages = this.messages.concat([{ role: 'user', content: text }]);
    this.busy = true;
    try {
      const answer = this.agentMode ? await this.runAgent(text) : await this.requestModel(text);
      this.messages = this.messages.concat([{ role: 'assistant', content: answer || 'Empty response.' }]);
      this.status = 'CloudAI request completed.';
    } catch (error) {
      this.messages = this.messages.concat([{ role: 'system', content: `Request failed: ${error}` }]);
      this.status = 'CloudAI request failed.';
    } finally {
      this.busy = false;
    }
  }

  private async requestModel(prompt: string): Promise<string> {
    const spec = JSON.parse(nativeCloudAI.prepareRequest(this.appDataDir, prompt)) as NativeRequest;
    const request = http.createHttp();
    try {
      const response = await request.request(spec.url, {
        method: http.RequestMethod.POST,
        header: { 'Content-Type': 'application/json', 'Authorization': spec.authorization },
        extraData: spec.body,
        expectDataType: http.HttpDataType.STRING
      });
      if (response.responseCode < 200 || response.responseCode >= 300) throw new Error(`HTTP ${response.responseCode}`);
      return nativeCloudAI.responseText(String(response.result));
    } finally {
      request.destroy();
    }
  }

  private actionJson(text: string): Record<string, string> | null {
    for (let start = 0; start < text.length; start++) {
      if (text.charAt(start) !== '{') continue;
      let depth = 0;
      let quoted = false;
      let escaped = false;
      for (let index = start; index < text.length; index++) {
        const current = text.charAt(index);
        if (quoted) {
          if (escaped) escaped = false;
          else if (current === '\\') escaped = true;
          else if (current === '"') quoted = false;
          continue;
        }
        if (current === '"') quoted = true;
        else if (current === '{') depth++;
        else if (current === '}') {
          depth--;
          if (depth === 0) {
            try {
              const value = JSON.parse(text.substring(start, index + 1)) as Record<string, string>;
              if (value && typeof value.action === 'string') return value;
            } catch (_) { break; }
          }
        }
      }
    }
    return null;
  }

  private sandboxPath(raw: string): string {
    const value = String(raw || '').trim();
    if (!value || value.includes('..') || value.includes('~')) return '';
    if (value.startsWith(this.appDataDir)) return value;
    return `${this.appDataDir}/${value.replace(/^\/+/, '')}`;
  }

  private executeAgentAction(action: Record<string, string>): string {
    const name = action.action.toLowerCase();
    if (name === 'read_file') {
      const path = this.sandboxPath(action.path || '');
      if (!path || !fs.accessSync(path)) return JSON.stringify({ ok: false, error: 'path_not_allowed' });
      const lowerPath = path.toLowerCase();
      const textFile = ['.txt', '.md', '.json', '.yaml', '.yml', '.xml', '.html', '.log', '.ini', '.cfg', '.toml', '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp', '.cs', '.go', '.rs', '.swift', '.kt', '.php', '.rb', '.sh', '.bat', '.ps1', '.sql', '.css'].some((suffix: string) => lowerPath.endsWith(suffix));
      if (!textFile) return JSON.stringify({ ok: false, error: 'unsupported_or_binary_file' });
      try {
        const file = fs.openSync(path, fs.OpenMode.READ_ONLY);
        const size = Math.min(Number(fs.statSync(path).size), 64000);
        const data = new ArrayBuffer(size);
        const length = fs.readSync(file.fd, data, { offset: 0, length: size });
        fs.closeSync(file);
        return JSON.stringify({ ok: true, path, content: buffer.from(data, 0, length).toString('utf-8'), truncated: Number(fs.statSync(path).size) > size });
      } catch (error) { return JSON.stringify({ ok: false, error: String(error) }); }
    }
    // HarmonyOS keeps the same protocol but intentionally does not expose a
    // shell or external URL launcher from the chat component.
    return JSON.stringify({ ok: false, error: 'unsupported_on_harmonyos', action: name });
  }

  private async runAgent(request: string): Promise<string> {
    const history = this.messages.slice(Math.max(0, this.messages.length - 10)).map((item: ChatMessage) => `${item.role}: ${item.content}`).join('\n');
    let transcript = 'You are CloudAI Agent Mode. Reply with one JSON object only. Actions: '
      + '{"action":"read_file","path":"relative/path"} or '
      + '{"action":"final","content":"answer"}. Files are restricted to the app sandbox; never run commands or claim external actions.\n\nTask:\n' + request;
    if (history) transcript += `\n\nConversation history:\n${history}`;
    let steps = 0;
    for (; steps < 12; steps++) {
      const answer = await this.requestModel(transcript);
      const action = this.actionJson(answer);
      if (!action) return answer;
      if (action.action.toLowerCase() === 'final') return `${action.content || ''}\n\nAgent steps: ${steps + 1}`;
      const result = this.executeAgentAction(action);
      transcript += `\n\nAssistant action: ${JSON.stringify(action)}\nAction result: ${result}\nContinue the task and return JSON.`;
      this.status = `CloudAI Agent step ${steps + 1}`;
    }
    return 'CloudAI Agent reached the 12-step safety limit.';
  }

  build() {
    Column() {
      Text('CloudAI v1.0.1')
        .fontSize(28)
        .fontWeight(FontWeight.Bold)
        .borderRadius(14)
      Text(this.status)
        .fontSize(16)
        .margin({ top: 12 })
      Row({ space: 8 }) {
        Select([{ value: 'OpenAI' }, { value: 'OpenAI Compatible' }, { value: 'OpenRouter' }, { value: 'DeepSeek' }, { value: 'SiliconFlow' }, { value: 'Custom' }])
          .selected(this.providerIndex()).onSelect((index: number) => {
            const codes: string[] = ['openai_official', 'openai_compatible', 'openrouter', 'deepseek', 'siliconflow', 'custom'];
            this.provider = codes[index] || 'custom';
            const defaultUrl = this.providerDefaults(this.provider);
            if (defaultUrl) this.baseUrl = defaultUrl;
          }).layoutWeight(1)
        TextInput({ placeholder: 'Model name', text: this.model }).layoutWeight(1).borderRadius(14).onChange((value: string) => this.model = value)
      }.width('92%').margin({ top: 18 })
      TextInput({ placeholder: 'Provider URL (Base URL or /chat/completions)', text: this.baseUrl })
        .width('92%').margin({ top: 10 }).borderRadius(14).onChange((value: string) => this.baseUrl = value)
      Row({ space: 8 }) {
        TextInput({ placeholder: 'API key', text: this.apiKey }).layoutWeight(1).borderRadius(14).onChange((value: string) => this.apiKey = value)
        Button('Save').borderRadius(14).onClick(() => this.saveProvider())
      }.width('92%').margin({ top: 18 })
      Row({ space: 8 }) {
        Text('Agent mode').fontSize(15)
        Toggle({ type: ToggleType.Switch, isOn: this.agentMode }).selectedColor('#2B9E70').onChange((value: boolean) => this.agentMode = value)
      }.width('92%').margin({ top: 12 })
      List({ space: 8 }) {
        ForEach(this.messages, (item: ChatMessage) => {
          ListItem() { Text(`${item.role}: ${item.content}`).width('100%').padding(12).borderRadius(14); }
        })
      }.layoutWeight(1).width('92%').margin({ top: 18 })
      Row({ space: 8 }) {
        TextArea({ placeholder: 'Message CloudAI', text: this.prompt }).layoutWeight(1).borderRadius(16).onChange((value: string) => this.prompt = value)
        Button(this.busy ? '...' : 'Send').enabled(!this.busy).borderRadius(14).onClick(() => this.send())
      }.width('92%').margin({ top: 12, bottom: 20 })
    }
    .width('100%')
    .height('100%')
    .justifyContent(FlexAlign.Center)
    .alignItems(HorizontalAlign.Center)
  }
}
