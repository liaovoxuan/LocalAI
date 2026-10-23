import { common } from '@kit.AbilityKit';
import { buffer } from '@kit.ArkTS';
import { fileIo as fs, picker } from '@kit.CoreFileKit';
import { HLlama } from 'hllama';

class ChatMessage {
  id: number;
  role: string;
  content: string;
  constructor(id: number, role: string, content: string) { this.id = id; this.role = role; this.content = content; }
}

@Entry
@Component
struct Index {
  @State private messages: ChatMessage[] = [];
  @State private input: string = '';
  @State private agentMode: boolean = false;
  @State private busy: boolean = false;
  @State private modelReady: boolean = false;
  @State private modelName: string = '';
  @State private attachmentName: string = '';
  private attachmentText: string = '';
  private contextId: string = '';
  private nextId: number = 1;

  aboutToAppear(): void {
    const context = getContext(this) as common.UIAbilityContext;
    void this.loadModel(`${context.filesDir}/models/localai-model.gguf`, 'localai-model.gguf', false);
  }

  private add(role: string, content: string): number {
    const id = this.nextId++;
    this.messages = this.messages.concat([new ChatMessage(id, role, content)]);
    return id;
  }

  private replace(id: number, content: string): void {
    this.messages = this.messages.map((item: ChatMessage) => item.id === id ? new ChatMessage(id, item.role, content) : item);
  }

  private async loadModel(path: string, name: string, report: boolean): Promise<void> {
    try {
      if (!fs.accessSync(path)) return;
      const id = await HLlama.initLlamaContext(path);
      if (!id || id <= 0) throw new Error('hllama returned an invalid context id');
      this.contextId = id.toString();
      this.modelReady = true;
      this.modelName = name;
      if (report) this.add('system', 'GGUF model loaded by hllama.');
    } catch (error) {
      this.contextId = '';
      this.modelReady = false;
      if (report) this.add('system', `Model load failed: ${error}`);
    }
  }

  private async pickModel(): Promise<void> {
    const context = getContext(this) as common.UIAbilityContext;
    try {
      const selected = await new picker.DocumentViewPicker(context).select();
      if (selected.length === 0 || !selected[0].toLowerCase().endsWith('.gguf')) { this.add('system', 'Select a GGUF model file.'); return; }
      const dir = `${context.filesDir}/models`;
      try { fs.mkdirSync(dir); } catch (_) { }
      const destination = `${dir}/localai-model.gguf`;
      fs.copyFileSync(selected[0], destination);
      await this.loadModel(destination, decodeURIComponent(selected[0].split('/').pop() || 'model.gguf'), true);
    } catch (error) { this.add('system', `Model selection failed: ${error}`); }
  }

  private async pickAttachment(): Promise<void> {
    const context = getContext(this) as common.UIAbilityContext;
    try {
      const selected = await new picker.DocumentViewPicker(context).select();
      if (selected.length === 0) return;
      const file = fs.openSync(selected[0], fs.OpenMode.READ_ONLY);
      const size = Math.min(Number(fs.statSync(selected[0]).size), 32768);
      const data = new ArrayBuffer(size);
      const length = fs.readSync(file.fd, data, { offset: 0, length: size });
      fs.closeSync(file);
      this.attachmentText = buffer.from(data, 0, length).toString('utf-8');
      this.attachmentName = decodeURIComponent(selected[0].split('/').pop() || 'attachment');
    } catch (error) { this.add('system', `Attachment failed: ${error}`); }
  }

  private send(): void {
    const content = this.input.trim();
    if (!content || this.busy) return;
    if (!this.modelReady || !this.contextId) { this.add('system', 'Select a GGUF model before sending a message.'); return; }
    this.input = '';
    let prompt = content;
    if (this.agentMode) prompt = 'Agent mode: use only supplied conversation and attachment text; never claim external actions.\n\n' + prompt;
    if (this.attachmentText) prompt += `\n\n[Attachment: ${this.attachmentName}]\n${this.attachmentText}`;
    this.add('user', content);
    this.attachmentName = '';
    this.attachmentText = '';
    const answerId = this.add('assistant', '');
    this.busy = true;
    try {
      HLlama.startCompletionSync(this.contextId, prompt,
        (finalOutput: string) => { this.replace(answerId, finalOutput || 'The model returned an empty response.'); this.busy = false; },
        (realtimeOutput: string) => this.replace(answerId, realtimeOutput));
    } catch (error) { this.replace(answerId, `Inference failed: ${error}`); this.busy = false; }
  }

  build() {
    Column() {
      Row() {
        Column({ space: 2 }) { Text('LocalAI').fontSize(24).fontWeight(FontWeight.Bold).fontColor('#F4F7F9'); Text('HarmonyOS / hllama').fontSize(12).fontColor('#8E9AA3'); }.alignItems(HorizontalAlign.Start)
        Blank(); Text(this.modelReady ? this.modelName : 'No model').fontSize(12).fontColor(this.modelReady ? '#7ED6B4' : '#F4B942').maxLines(1)
      }.width('100%').height(70).padding({ left: 20, right: 20 }).backgroundColor('#101418')
      Row({ space: 10 }) { Button('New').onClick(() => { this.messages = []; }); Button('Model').onClick(() => this.pickModel()); Blank(); Text('Agent').fontColor(this.agentMode ? '#7ED6B4' : '#ABB5BC'); Toggle({ type: ToggleType.Switch, isOn: this.agentMode }).selectedColor('#2B9E70').onChange((value: boolean) => this.agentMode = value); }.width('100%').height(58).padding({ left: 20, right: 20 }).backgroundColor('#151B20')
      List({ space: 10 }) { ForEach(this.messages, (item: ChatMessage) => { ListItem() { Column({ space: 6 }) { Text(item.role === 'user' ? 'YOU' : item.role === 'assistant' ? 'LOCALAI' : 'SYSTEM').fontSize(11).fontWeight(FontWeight.Bold).fontColor('#7ED6B4'); Text(item.content).fontSize(15).fontColor('#E8EDF2').lineHeight(22).width('100%'); }.width('100%').padding(14).backgroundColor(item.role === 'user' ? '#1D2A31' : '#171D22').border({ width: 1, color: '#303A41' }).borderRadius(6); } }, (item: ChatMessage) => item.id.toString()); }.layoutWeight(1).width('100%').padding(14).backgroundColor('#11161A')
      Column({ space: 8 }) { if (this.attachmentName) Text(`Attachment: ${this.attachmentName}`).fontSize(12).fontColor('#7ED6B4'); Row({ space: 10 }) { Button('+').width(44).height(44).fontSize(24).onClick(() => this.pickAttachment()); TextArea({ text: this.input, placeholder: 'Message LocalAI' }).layoutWeight(1).height(76).fontColor('#EDF2F5').backgroundColor('#0D1114').onChange((value: string) => this.input = value); Button(this.busy ? '...' : 'Send').width(74).height(44).enabled(!this.busy).onClick(() => this.send()); }.width('100%'); }.width('100%').padding(14).backgroundColor('#151B20')
    }.width('100%').height('100%').backgroundColor('#11161A')
  }
}
