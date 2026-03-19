# 網站版本差異比較（文字 / 圖片）
本公司的pm規格文件最後會發布為靜態網頁，由於常常文件會被異動多次，導致開發人員不確定新舊文件的差異，在閱讀文件上會耗費較多的時間，為了節省比對文件的時間，使用cursor codex5.3開發網頁內容比對，希望可以達成節省閱讀時間與人工比對文件內容的心力為目標

# 說明

這份文件說明 `site_diff_visualizer.py` 的使用方式。  
可比較兩個離線快照資料夾，並輸出視覺化報告。

## 1) 基本用法

```bash
python site_diff_visualizer.py "<舊版資料夾>" "<新版資料夾>" -o diff_report
```

例如：

```bash
python site_diff_visualizer.py "D:\tfs\WebDownload\委託自動申調-第二階段\20260301" "D:\tfs\WebDownload\委託自動申調-第二階段\20260309" -o "D:\tfs\WebDownload\委託自動申調-第二階段\diff_20260309"
```

## 2) 輸出內容

- `report.html`：可視覺化檢視差異
- `report.json`：機器可讀的差異結果
- `assets/`：圖片差異預覽（舊圖 / 新圖 / 高亮圖）
- 終端機顯示比較進度條（百分比 / 已處理檔案數）

## 3) 差異項目

- 文字檔：顯示左右對照差異（舊版 / 新版並排、行內高亮）
- 圖片檔：比較檔案差異與預覽圖
- 頁面視覺：自動截圖後左右對照（類似人工開兩個視窗比對）
- 頁面視覺區塊會顯示資源載入失敗統計（失敗頁數 / 失敗請求數）
- 其他二進位檔：顯示 hash 差異

## 4) 圖片高亮差異（可選）

若有安裝 Pillow，會產生紅色高亮差異圖：

```bash
pip install pillow
```

## 5) 參數

- `-o, --output`：報告輸出資料夾（預設 `diff_report`）
- `--text-context-lines`：文字 diff 顯示前後文行數（預設 2）
- `--max-text-diff-lines`：每個檔案側邊對照最多顯示幾列（預設 300）
- `--no-progress`：關閉終端機進度條顯示
- `--no-page-visual`：關閉頁面截圖視覺比對
- `--max-visual-pages`：最多比對幾個 HTML 頁面（預設 40）
- `--visual-width` / `--visual-height`：頁面截圖視窗大小（預設 1366x900）
- `--visual-wait-ms`：頁面載入後額外等待毫秒（預設 800）
