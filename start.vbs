' AlphaGPT客户端启动器
' 双击本文件或为其创建桌面快捷方式即可。
' 桌面快捷方式右键→目标指向本 .vbs，可实现"双击图标 → 后台起服务 → 自动开浏览器"。
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
' cwd 定位到本脚本所在目录（client/），保证 run.py 的相对路径正确
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
' 第二参数 0 = 隐藏窗口；第三参数 False = 不等待（wscript 立即退出）
sh.Run "pythonw run.py", 0, False
' 若发布到 pythonw 不在 PATH 的机器，把上一行改为随包 pythonw 的绝对/相对路径，例如：
'   sh.Run "..\python\pythonw.exe run.py", 0, False
