// Command nterender 是出图侧车：**一次调用画一张图**，画完就退出。
//
// 为什么不做常驻进程：出图是「一次一张、画完就完」的活，一张图几十毫秒，
// 常驻省下的那点启动开销还不够多维护一套协议与超时回收。而一次一进程的好处是
// 崩了不会连累主进程、也不会有「上一个请求卡住」这种状态。
//
// 协议（stdin 一行 JSON，stdout 一行 JSON）：
//
//	{"kind":"event|result|help|share","site":"https://…","out":"/path/x.png","payload":{…}}
//	{"ok":true,"width":1080,"height":2368,"bytes":421337}
//	{"ok":false,"error":"..."}
//
// 诊断信息一律走 stderr，Python 侧会转进自己的日志。
package main

import (
	"encoding/json"
	"fmt"
	"image"
	"image/jpeg"
	"image/png"
	"io"
	"os"
	"path/filepath"
	"time"

	"ntematch/ntecalc/internal/render"
)

// buildTag 在编译期用 -ldflags "-X main.buildTag=..." 覆盖，便于现场核对版本。
var buildTag = "dev"

// Protocol 是前后端约定的协议版本：**加字段不改版本，改语义才加**。
const Protocol = 2

type request struct {
	Kind    string          `json:"kind"`
	Site    string          `json:"site"`
	Out     string          `json:"out"`
	Quality int             `json:"quality"`
	Payload json.RawMessage `json:"payload"`
}

type response struct {
	OK     bool   `json:"ok"`
	Error  string `json:"error,omitempty"`
	Width  int    `json:"width,omitempty"`
	Height int    `json:"height,omitempty"`
	Bytes  int    `json:"bytes,omitempty"`
	Font   string `json:"font,omitempty"`
}

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "--version":
			fmt.Printf("nterender %d %s\n", Protocol, buildTag)
			return
		case "--font":
			if err := render.LoadFonts(); err != nil {
				fmt.Fprintf(os.Stderr, "没有可用的中文字体: %v\n", err)
				os.Exit(1)
			}
			fmt.Println(render.FontPath())
			return
		case "--selftest":
			if err := selftest(); err != nil {
				fmt.Fprintf(os.Stderr, "selftest failed: %v\n", err)
				os.Exit(1)
			}
			fmt.Println("selftest ok")
			return
		default:
			fmt.Fprintf(os.Stderr, "unknown flag: %s\n", os.Args[1])
			os.Exit(2)
		}
	}

	raw, err := io.ReadAll(io.LimitReader(os.Stdin, 8<<20))
	if err != nil {
		fail(fmt.Errorf("读 stdin 失败: %w", err))
	}
	var req request
	if err := json.Unmarshal(raw, &req); err != nil {
		fail(fmt.Errorf("请求不是合法 JSON: %w", err))
	}
	if req.Out == "" {
		fail(fmt.Errorf("缺少 out 路径"))
	}
	started := time.Now()
	resp, err := run(req)
	if err != nil {
		fail(err)
	}
	resp.Font = render.FontPath()
	writeJSON(resp)
	fmt.Fprintf(os.Stderr, "nterender ok | kind=%s | %dx%d | %dms\n",
		req.Kind, resp.Width, resp.Height, time.Since(started).Milliseconds())
}

func run(req request) (*response, error) {
	var img *image.RGBA
	switch req.Kind {
	case "event":
		var payload render.EventPayload
		if err := json.Unmarshal(req.Payload, &payload); err != nil {
			return nil, fmt.Errorf("event 内容不合法: %w", err)
		}
		img = render.RenderEvent(req.Site, payload)
	case "result":
		var payload render.ResultPayload
		if err := json.Unmarshal(req.Payload, &payload); err != nil {
			return nil, fmt.Errorf("result 内容不合法: %w", err)
		}
		img = render.RenderResult(req.Site, payload)
	case "help":
		var payload render.HelpPayload
		if err := json.Unmarshal(req.Payload, &payload); err != nil {
			return nil, fmt.Errorf("help 内容不合法: %w", err)
		}
		img = render.RenderHelp(payload)
	case "share":
		var payload render.SharePayload
		if err := json.Unmarshal(req.Payload, &payload); err != nil {
			return nil, fmt.Errorf("share 内容不合法: %w", err)
		}
		img = render.RenderShare(payload)
	default:
		return nil, fmt.Errorf("未知的 kind: %q", req.Kind)
	}
	if img == nil {
		return nil, fmt.Errorf("渲染失败（没有可用的中文字体？）")
	}

	// 先写临时文件再改名：中途崩了也不会让调用方读到半张图
	tmp := req.Out + ".tmp"
	if err := os.MkdirAll(filepath.Dir(req.Out), 0o755); err != nil {
		return nil, err
	}
	f, err := os.Create(tmp)
	if err != nil {
		return nil, err
	}
	if req.Kind == "help" {
		quality := req.Quality
		if quality <= 0 || quality > 100 {
			quality = 92
		}
		err = jpeg.Encode(f, img, &jpeg.Options{Quality: quality})
	} else {
		err = png.Encode(f, img)
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		_ = os.Remove(tmp)
		return nil, err
	}
	if err := os.Rename(tmp, req.Out); err != nil {
		_ = os.Remove(tmp)
		return nil, err
	}
	stat, err := os.Stat(req.Out)
	if err != nil {
		return nil, err
	}
	b := img.Bounds()
	return &response{OK: true, Width: b.Dx(), Height: b.Dy(), Bytes: int(stat.Size())}, nil
}

func fail(err error) {
	writeJSON(&response{OK: false, Error: err.Error()})
	fmt.Fprintf(os.Stderr, "nterender 失败: %v\n", err)
	os.Exit(1)
}

func writeJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

// selftest 只验证「字体找得到、能上色、能编码」，不写任何文件。
func selftest() error {
	if err := render.LoadFonts(); err != nil {
		return err
	}
	img := render.RenderShare(render.SharePayload{Title: "自检", Tagline: "SELFTEST", Note: "ok"})
	if img == nil || img.Bounds().Dx() != 1200 {
		return fmt.Errorf("分享图尺寸不对")
	}
	fmt.Fprintf(os.Stderr, "font: %s\n", render.FontPath())
	return nil
}
