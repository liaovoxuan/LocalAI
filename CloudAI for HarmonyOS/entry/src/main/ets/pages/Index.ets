@Entry
@Component
struct Index {
  build() {
    Column() {
      Text('CloudAI v1.0.0')
        .fontSize(28)
        .fontWeight(FontWeight.Bold)
      Text('HarmonyOS C++ core is ready.')
        .fontSize(16)
        .margin({ top: 12 })
    }
    .width('100%')
    .height('100%')
    .justifyContent(FlexAlign.Center)
    .alignItems(HorizontalAlign.Center)
  }
}
