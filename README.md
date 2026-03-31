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

- `report.html`：可視覺化檢視差異（點擊圖片可放大檢視）
- `report.json`：機器可讀的差異結果
- `assets/`：頁面截圖與差異標注圖
- 終端機顯示比較進度條（百分比 / 已處理檔案數）
- 預設為「快速模式」：只做頁面視覺比對，略過檔案層級 hash/文字/圖片詳細比對，且不顯示「新增 / 刪除 / 未變更 / 其他二進位」區塊，以提升速度

## 3) 差異項目

- 文字檔：顯示左右對照差異（舊版 / 新版並排、行內高亮）
- 圖片檔：比較檔案差異與預覽圖
- 頁面視覺：自動截圖後左右對照，並透過文字比對自動標注差異：
  - 新版橘框：新增或異動的內容
  - 舊版紅框：已刪除的內容
- 頁面視覺區塊會顯示資源載入失敗統計（失敗頁數 / 失敗請求數）
- 其他二進位檔：顯示 hash 差異

## 4) 相依套件（可選）

- **Pillow**：產生差異標注圖（橘框 / 紅框疊加於截圖上）

```bash
pip install pillow
```

- **Playwright**：頁面截圖視覺比對（自動開啟瀏覽器截取完整頁面）

```bash
pip install playwright
python -m playwright install chromium
```

未安裝時對應功能會自動停用，報告頂部會顯示提示。

## 5) 參數

- `-o, --output`：報告輸出資料夾（預設 `diff_report`）
- `--text-context-lines`：文字 diff 顯示前後文行數（預設 2）
- `--max-text-diff-lines`：每個檔案側邊對照最多顯示幾列（預設 300）
- `--no-progress`：關閉終端機進度條顯示
- `--no-page-visual`：關閉頁面截圖視覺比對
- `--max-visual-pages`：最多比對幾個 HTML 頁面（預設 40）
- `--visual-width` / `--visual-height`：頁面截圖視窗大小（預設 1366x900）
- `--visual-wait-ms`：頁面載入後額外等待毫秒（預設 200）
- `--full-report`：改回完整模式（啟用文字異動 / 圖片異動詳細比較，並顯示新增 / 刪除 / 未變更）
