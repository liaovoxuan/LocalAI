import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { window } from '@kit.ArkUI';
import test from 'libcaptest.so';

const DOMAIN: number = 0x0000;
const TAG: string = 'NativeCapTest';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    hilog.info(DOMAIN, TAG, 'Ability onCreate');
    try {
      const filesDir: string = this.context.filesDir;
      const ret: number = test.RunCapTests(filesDir);
      hilog.info(DOMAIN, TAG, 'RunCapTests returned %{public}d', ret);
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'RunCapTests failed: %{public}s', JSON.stringify(e));
    }
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        hilog.error(DOMAIN, TAG, 'loadContent failed: %{public}s', JSON.stringify(err));
      }
    });
  }
}
