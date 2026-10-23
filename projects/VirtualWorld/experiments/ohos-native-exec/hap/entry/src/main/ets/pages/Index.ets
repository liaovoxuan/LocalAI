@Entry
@Component
struct Index {
  @State message: string = 'Native Cap Test';

  build() {
    Row() {
      Column() {
        Text(this.message)
          .fontSize(30)
          .fontWeight(400)
      }
      .width('100%')
    }
    .height('100%')
  }
}
