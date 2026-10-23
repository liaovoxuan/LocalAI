import { common } from '@kit.AbilityKit';
import { fileIo as fs, picker } from '@kit.CoreFileKit';
import { HLlama } from 'hllama';

class TerminalLine { id: number; text: string; color: string; constructor(id: number, text: string, color: string) { this.id = id; this.text = text; this.color = color; } }

@Entry
@Component
struct Index {
  @State private lines: TerminalLine[] = [];
  @State private command: string = '';
  @State private busy: boolean = false;
  @State private agentMode: boolean = false;
  @State private modelName: string = 'no model';
  private contextId: string = '';
  private nextId: number = 1;

  aboutToAppear(): void { this.write('LocalAI SE / HarmonyOS / hllama only', '#72D3A5'); this.write('Use /model, /agent, /new or enter a prompt.', '#9AA7B0'); }
  private write(text: string, color: string = '#DDE5E9'): void { this.lines = this.lines.concat([new TerminalLine(this.nextId++, text, color)]); }

  private async chooseModel(): Promise<void> {
    const context = getContext(this) as common.UIAbilityContext;
    try {
      const selected = await new picker.DocumentViewPicker(context).select();
      if (selected.length === 0 || !selected[0].toLowerCase().endsWith('.gguf')) { this.write('ERROR: select a .gguf model file.', '#F2BA5C'); return; }
      const dir = `${context.filesDir}/models`;
      try { fs.mkdirSync(dir); } catch (_) { }
      const path = `${dir}/localai-se-model.gguf`;
      fs.copyFileSync(selected[0], path);
      const id = await HLlama.initLlamaContext(path);
      if (!id || id <= 0) throw new Error('hllama returned an invalid context id');
      this.contextId = id.toString();
      this.modelName = decodeURIComponent(selected[0].split('/').pop() || 'model.gguf');
      this.write(`MODEL: ${this.modelName}`, '#72D3A5');
    } catch (error) { this.write(`ERROR: ${error}`, '#F28080'); }
  }

  private reset(): void { this.contextId = ''; this.modelName = 'no model'; this.lines = []; this.write('Conversation reset.', '#9AA7B0'); }
  private submit(): void {
    const input = this.command.trim();
    if (!input || this.busy) return;
    this.command = '';
    if (input === '/help') { this.write('/model select GGUF   /agent toggle Agent   /new reset', '#9AA7B0'); return; }
    if (input === '/model') { void this.chooseModel(); return; }
    if (input === '/agent') { this.agentMode = !this.agentMode; this.write(`AGENT: ${this.agentMode ? 'ON' : 'OFF'}`, '#72D3A5'); return; }
    if (input === '/new') { this.reset(); return; }
    if (!this.contextId) { this.write('ERROR: no model loaded. Enter /model first.', '#F2BA5C'); return; }
    let prompt = input;
    if (this.agentMode) prompt = 'Agent mode: use only supplied conversation; never claim external actions.\n\n' + prompt;
    this.write(`> ${input}`, '#9BC4FF');
    this.busy = true;
    try {
      HLlama.startCompletionSync(this.contextId, prompt,
        (finalOutput: string) => { this.write(finalOutput || 'The model returned an empty response.'); this.busy = false; },
        (realtimeOutput: string) => { if (realtimeOutput) this.write(realtimeOutput, '#B9C5CC'); });
    } catch (error) { this.write(`ERROR: ${error}`, '#F28080'); this.busy = false; }
  }

  build() {
    Column() {
      Row() { Text('LOCALAI SE').fontSize(20).fontWeight(FontWeight.Bold).fontColor('#72D3A5'); Blank(); Text(this.agentMode ? 'AGENT ON' : 'AGENT OFF').fontSize(12).fontColor('#9AA7B0'); }.width('100%').padding({ left: 16, right: 16, top: 18, bottom: 12 }).backgroundColor('#101418')
      Row({ space: 8 }) { Button('Model').onClick(() => this.chooseModel()); Button('/agent').onClick(() => { this.command = '/agent'; this.submit(); }); Button('/new').onClick(() => this.reset()); Blank(); Text(this.modelName).fontSize(11).fontColor('#9AA7B0').maxLines(1); }.width('100%').padding({ left: 16, right: 16, bottom: 12 }).backgroundColor('#101418')
      List({ space: 7 }) { ForEach(this.lines, (line: TerminalLine) => { ListItem() { Text(line.text).fontSize(14).fontColor(line.color).fontFamily('monospace').width('100%').padding({ left: 16, right: 16 }); } }, (line: TerminalLine) => line.id.toString()); }.layoutWeight(1).width('100%').padding({ top: 16, bottom: 16 }).backgroundColor('#080B0D')
      Row({ space: 8 }) { Text('>').fontColor('#72D3A5').fontSize(22).fontFamily('monospace'); TextInput({ text: this.command, placeholder: 'message or /help' }).layoutWeight(1).fontColor('#E7EEF1').backgroundColor('#141B20').onChange((value: string) => this.command = value).onSubmit(() => this.submit()); Button(this.busy ? '...' : 'Run').enabled(!this.busy).onClick(() => this.submit()); }.width('100%').padding(14).backgroundColor('#101418')
    }.width('100%').height('100%').backgroundColor('#080B0D')
  }
}
