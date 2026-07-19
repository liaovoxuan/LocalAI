from plugins.qemu_bridge.standalone import main


if __name__ == "__main__":
    main()


# LocalAI v0.8 QEMU Bridge GUI
def launch_qemu_bridge_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    win = tk.Tk()
    win.title("QEMU Bridge 0.8")
    win.geometry("720x480")

    tk.Label(win, text="QEMU Bridge 0.8",
             font=("Arial", 18, "bold")).pack(pady=15)

    path_var = tk.StringVar()

    frame = tk.Frame(win)
    frame.pack(fill="x", padx=20)

    tk.Entry(frame, textvariable=path_var).pack(side="left", fill="x", expand=True)

    def choose():
        path_var.set(filedialog.askopenfilename())

    tk.Button(frame, text="选择配置", command=choose).pack(side="left", padx=8)

    result = tk.Text(win, height=12)
    result.pack(fill="both", expand=True, padx=20, pady=15)

    def convert():
        result.insert("end", "已加载配置：\n" + path_var.get() + "\n")
        result.insert("end", "转换功能调用 QEMU Bridge 核心模块。\n")

    tk.Button(win, text="转换", command=convert).pack(pady=5)

    win.mainloop()
