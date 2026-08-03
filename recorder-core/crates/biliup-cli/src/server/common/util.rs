use crate::server::errors::{AppError, AppResult};
use crate::server::infrastructure::models::StreamerInfo;
use crate::server::infrastructure::models::hook_step::HookStep;
use chrono::{Duration, Local};
use error_stack::{ResultExt, bail};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use tracing::{error, info};
use url::Url;

/// 录制器配置结构体
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Recorder {
    ///  filename_prefix   文件名前缀模板
    pub filename_prefix: Option<String>,
    /// 直播间信息
    pub streamer_info: StreamerInfo,
}

impl Recorder {
    pub fn new(filename_prefix: Option<String>, streamer_info: StreamerInfo) -> Self {
        Self {
            filename_prefix,
            streamer_info,
        }
    }

    /// 生成文件名模板（包含时间格式占位符），并清洗非法字符
    pub fn filename_template(&self) -> String {
        let raw = if let Some(prefix) = &self.filename_prefix {
            self.template_with(prefix)
        } else {
            format!("{}%Y-%m-%dT%H_%M_%S", self.streamer_info.name)
        };
        sanitize_filename(&raw)
    }

    fn template_with(&self, template: &str) -> String {
        let live_start = self
            .streamer_info
            .date
            .with_timezone(&Local)
            .format("%Y-%m-%d_%H-%M")
            .to_string();
        template
            .replace("{streamer}", &self.streamer_info.name)
            .replace("{title}", &self.streamer_info.title)
            .replace("{url}", &self.streamer_info.url)
            .replace("{live_start}", &live_start)
    }

    /// 生成“基名”（不带扩展名），时间冲突时按秒+1继续尝试，直到唯一
    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        let mut t = Local::now();

        loop {
            let base = t.format(&template).to_string();
            if !self.exists_with_suffix(&base, suffix) {
                return base;
            }
            t += Duration::seconds(1);
        }
    }

    /// 生成“基名”（不带扩展名）
    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&template)
            .to_string()
    }

    pub fn format(&self, template: &str) -> String {
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&self.template_with(template))
            .to_string()
    }

    /// 直接生成带扩展名的完整路径（当前目录下）
    pub fn generate_path(&self, suffix: &str) -> PathBuf {
        append_suffix(&self.generate_filename(suffix), suffix)
    }

    fn exists_with_suffix(&self, base: &str, suffix: &str) -> bool {
        append_suffix(base, suffix).exists()
    }
}

fn append_suffix(base: &str, suffix: &str) -> PathBuf {
    PathBuf::from(format!("{base}.{}", suffix.trim_start_matches('.')))
}

/// 路径模板清洗。
/// - 允许 `/` 创建场次目录，但移除空目录、`.` 和 `..`
/// - 替换各目录名中的非法字符为 '_'；去掉末尾空格与点
/// - 保留 '%'，以便 strftime 能正常工作
fn sanitize_filename(name: &str) -> String {
    let normalized = name.replace('\\', "/");
    let windows_drive = normalized
        .as_bytes()
        .get(0..3)
        .filter(|prefix| prefix[0].is_ascii_alphabetic() && prefix[1] == b':' && prefix[2] == b'/')
        .map(|_| &normalized[..2]);
    let is_absolute = normalized.starts_with('/') || windows_drive.is_some();
    let path_body = windows_drive.map_or(normalized.as_str(), |_| &normalized[3..]);
    let parts: Vec<String> = path_body
        .split('/')
        .filter(|part| !part.is_empty() && *part != "." && *part != "..")
        .map(|part| {
            let mut out = String::with_capacity(part.len());
            for ch in part.chars() {
                match ch {
                    ':' | '*' | '?' | '"' | '<' | '>' | '|' => out.push('_'),
                    c if c.is_control() => out.push('_'),
                    _ => out.push(ch),
                }
            }
            let out = out.trim_end_matches([' ', '.']).to_string();
            if out.is_empty() { "_".to_string() } else { out }
        })
        .collect();
    if parts.is_empty() {
        "_".to_string()
    } else if let Some(drive) = windows_drive {
        format!("{drive}/{}", parts.join("/"))
    } else if is_absolute {
        format!("/{}", parts.join("/"))
    } else {
        parts.join("/")
    }
}

/// 生成弹幕文件名模板（包含时间格式占位符），并清洗非法字符
pub fn danmaku_filename_template(
    filename_prefix: Option<&str>,
    streamer_info: &StreamerInfo,
) -> String {
    Recorder::new(filename_prefix.map(str::to_string), streamer_info.clone()).filename_template()
}

/// 从 URL 中提取媒体扩展名（小写），例如 "flv", "mp4" 等。
/// 先尝试解析 URL 的 path 的扩展名；如果没有，再查 query 中常见的参数（format/type/ext）。
/// 返回 None 表示无法判断。
pub fn media_ext_from_url(input: &str) -> Option<String> {
    // 统一的扩展名清洗：去空白/前导点，切掉 MIME/分隔符，并转小写
    fn clean_ext(val: &str) -> Option<String> {
        let token = val
            .trim()
            .trim_start_matches('.') // .mp4 -> mp4
            .split(['/', ';', ',', '?', '&', '#']) // video/mp4;codecs=...
            .next()
            .map(str::trim)
            .unwrap_or("");
        if token.is_empty() {
            None
        } else {
            Some(token.to_ascii_lowercase())
        }
    }

    // 1) 先尝试按 URL 解析
    if let Ok(url) = Url::parse(input) {
        // a) 从最后一个 path segment 取扩展名
        if let Some(seg) = url.path_segments().and_then(|mut s| s.next_back())
            && let Some((_, ext)) = seg.rsplit_once('.')
            && let Some(ext) = clean_ext(ext)
        {
            return Some(ext);
        }

        // b) 常见 query 参数中找一次（不重复多轮扫描），忽略大小写
        let keys = ["format", "type", "ext", "filetype", "fmt"];
        if let Some(ext) = url.query_pairs().find_map(|(k, v)| {
            if keys.iter().any(|t| k.as_ref().eq_ignore_ascii_case(t)) {
                clean_ext(&v)
            } else {
                None
            }
        }) {
            return Some(ext);
        }

        return None;
    }

    // 2) 不是完整 URL 的兜底：纯字符串/相对地址
    let before_q = input.split('?').next().unwrap_or(input);
    if let Some((_, ext)) = before_q.rsplit_once('.') {
        return clean_ext(ext);
    }

    None
}

pub fn parse_time(segment_time: &str) -> std::time::Duration {
    let parts: Vec<&str> = segment_time.split(':').collect();
    let h = parts[0].parse::<i32>().unwrap_or(1);
    let m = parts[1].parse::<i32>().unwrap_or(0);
    let s = parts[2].parse::<i32>().unwrap_or(0);
    std::time::Duration::from_secs((h * 3600 + m * 60 + s) as u64)
}

#[cfg(test)]
mod tests {
    use super::{Recorder, sanitize_filename};
    use crate::server::common::util::media_ext_from_url;
    use crate::server::infrastructure::models::StreamerInfo;
    use chrono::{Local, TimeZone, Utc};
    use std::path::PathBuf;

    #[test]
    fn it_works() {
        assert_eq!(
            media_ext_from_url(
                "https://hwa.douyucdn2.cn/live/6512r9pAbb5Ercd1.flv?wsAuth=c77de01c8fcbc7b04b3d6daf66e523f5&token=web-h5-0-6512-f52253ea808109b3e2b66f385345c5e4ebdd692a847af73b&logo=0&expire=0&did=b6b79db91ca484562dcd6a1d5cdd9639&ver=219032101&pt=2&st=0&sid=420338944&mcid2=0&origin=dy&fcdn=hw&fo=0&mix=0&isp="
            ),
            Some("flv".to_string())
        );
    }

    #[test]
    fn recording_template_supports_one_folder_per_live_session() {
        let info = StreamerInfo::new(
            "YYF",
            "https://www.douyu.com/9999",
            "陪伴每一天: DOTA2",
            Utc.with_ymd_and_hms(2026, 7, 26, 12, 30, 0).unwrap(),
            "",
        );
        let live_start = info
            .date
            .with_timezone(&Local)
            .format("%Y-%m-%d_%H-%M")
            .to_string();
        let recorder = Recorder::new(
            Some(
                "{streamer}_{title}_{live_start}/{streamer}_{title}_%Y-%m-%d_%H-%M"
                    .to_string(),
            ),
            info,
        );
        let template = recorder.filename_template();

        assert_eq!(
            template,
            format!(
                "YYF_陪伴每一天_ DOTA2_{live_start}/YYF_陪伴每一天_ DOTA2_%Y-%m-%d_%H-%M"
            )
        );
    }

    #[test]
    fn recording_template_preserves_absolute_output_root() {
        assert_eq!(
            sanitize_filename("/data/recordings/YYF/直播:回放_%Y-%m-%d"),
            "/data/recordings/YYF/直播_回放_%Y-%m-%d"
        );
    }

    #[test]
    fn recording_template_preserves_windows_absolute_output_root() {
        assert_eq!(
            sanitize_filename(r"C:\recordings\YYF\直播:回放_%Y-%m-%d"),
            "C:/recordings/YYF/直播_回放_%Y-%m-%d"
        );
    }

    #[test]
    fn recording_path_preserves_dots_inside_live_title() {
        let info = StreamerInfo::new(
            "国民大舅哥",
            "https://www.douyu.com/182102",
            "8号10.5开团",
            Utc.with_ymd_and_hms(2026, 8, 1, 21, 29, 0).unwrap(),
            "",
        );
        let recorder = Recorder::new(Some("{streamer}_{title}".to_string()), info);

        assert_eq!(
            recorder.generate_path("flv"),
            PathBuf::from("国民大舅哥_8号10.5开团.flv")
        );
    }
}

/// 文件验证配置
#[derive(Clone)]
pub struct FileValidator {
    min_size: u64,
    check_format: bool,
}

impl FileValidator {
    pub fn new(min_size: u64, check_format: bool) -> Self {
        Self {
            min_size,
            check_format,
        }
    }
}

impl Default for FileValidator {
    fn default() -> Self {
        Self {
            min_size: 1024 * 1024 * 100, // 100MB minimum
            check_format: true,
        }
    }
}

impl FileValidator {
    /// 验证文件有效性
    pub fn validate(&self, path: &Path) -> AppResult<()> {
        let metadata = fs::metadata(path).change_context(AppError::Unknown)?;

        let size = metadata.len();

        if size < self.min_size {
            let display = path.display();
            let path = path.to_owned();
            tokio::spawn(async move {
                let Ok(()) = HookStep::remove_file(&[&path])
                    .await
                    .inspect_err(|e| error!(e=?e))
                else {
                    return;
                };
                info!("过滤删除 - {}", path.display());
            });
            bail!(AppError::Custom(format!(
                "File {display} too small: {size} bytes, minimum: {} bytes",
                self.min_size
            )));
        }

        // 可选：检查文件格式
        if self.check_format {
            self.validate_format(path)?;
        }

        Ok(())
    }

    fn validate_format(&self, path: &Path) -> AppResult<()> {
        // 简单的格式验证 - 检查扩展名
        if let Some(extension) = path.extension() {
            let ext = extension.to_string_lossy().to_lowercase();
            match ext.as_str() {
                "mp4" | "flv" | "ts" | "m3u8" | "mkv" => Ok(()),
                _ => bail!(AppError::Custom(format!("Unsupported format: {}", ext))),
            }
        } else {
            bail!(AppError::Custom("No file extension found".to_string()))
        }
    }
}
