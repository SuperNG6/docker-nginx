# Docker Nginx

## 简介

#### 基于官方nginx debian:buster-slim 镜像编译
添加`ngx_cache_purge`、`ngx_brotli`、 `ngx_pagespeed`等第三方模块  

### 更新日志
#### 2022年8月1日
1.22.0 版取消ngx_cache_purge模块，ngx_cache_purge的意义越来越小了，并且对最新版NGINX存在兼容性问题，故取消
有需要的可以下载1.20.2版

### 注意：请在自己的conf文件中加载模块

````
load_module /usr/local/nginx/modules/ngx_http_brotli_filter_module.so;
load_module /usr/local/nginx/modules/ngx_http_brotli_static_module.so;
load_module /usr/local/nginx/modules/ngx_http_cache_purge_module.so;
<!-- load_module /usr/local/nginx/modules/ngx_pagespeed.so; -->
````

### brotli 配置文件

````
    brotli on;
    brotli_comp_level 5; 
	brotli_static on;
    brotli_types
		application/atom+xml
		application/javascript
		application/json
		application/rss+xml
		application/vnd.ms-fontobject
		application/x-font-ttf
		application/x-font-opentype
		application/x-font-truetype
		application/x-javascript
		application/x-web-app-manifest+json
		application/xhtml+xml
		application/xml
		font/eot
		font/opentype
		font/otf
		image/svg+xml
		image/x-icon
		image/vnd.microsoft.icon
		text/css
		text/plain
		text/javascript
		text/x-component;
````

### pagespeed配置文件
````
    ####基本设置######
    pagespeed on;
    pagespeed FileCachePath /var/ngx_pagespeed_cache;
    # 禁用CoreFilters    
    pagespeed RewriteLevel PassThrough;
    # 一个标识而已（若在浏览器开发者工具里的链接请求响应标头看到此标识，则说明 PageSpeed 生效）
    pagespeed XHeaderValue "Powered By sleele.com";
    # HTML页面链接转小写（SEO 优化，推荐）
    pagespeed LowercaseHtmlNames on;
    # 启用压缩空白过滤器    
    pagespeed EnableFilters collapse_whitespace;    
    # 启用JavaScript库卸载,有副作用
    # pagespeed EnableFilters canonicalize_javascript_libraries;  
    # 把多个CSS文件合并成一个CSS文件    
    # pagespeed EnableFilters combine_css;    
    # 把多个JavaScript文件合并成一个JavaScript文件    
    # pagespeed EnableFilters combine_javascript;    
    # 删除带默认属性的标签    
    pagespeed EnableFilters elide_attributes;    
    # 改善资源的可缓存性    
    pagespeed EnableFilters extend_cache;    
    # 更换被导入文件的@import，精简CSS文件    
    pagespeed EnableFilters flatten_css_imports;    
    pagespeed CssFlattenMaxBytes 5120;    
    # 延时加载客户端看不见的图片    
    pagespeed EnableFilters lazyload_images;    
    # 启用JavaScript缩小机制    
    pagespeed EnableFilters rewrite_javascript;    
    # 预解析DNS查询    
    pagespeed EnableFilters insert_dns_prefetch;    
    # 重写CSS，首先加载渲染页面的CSS规则    
    pagespeed EnableFilters prioritize_critical_css; 
    # Example 禁止pagespeed 处理/wp-admin/目录(可选配置，可参考使用)
    pagespeed Disallow "*/wp-admin/*";
    pagespeed Disallow "*/wp-adminlogin/*";

    #######图片处理配置########
    # 延时加载图片
    pagespeed EnableFilters lazyload_images;
    # 不加载显示区域以外的图片
    pagespeed LazyloadImagesAfterOnload off;
    pagespeed LazyloadImagesBlankUrl "https://cdn.jsdelivr.net/gh/SuperNG6/pic@master/uPic/touxiangimage.jpg";
    # 启用图片优化机制(主要是 inline_images, recompress_images, convert_to_webp_lossless（这个命令会把PNG和静态Gif图片转化为webp）, and resize_images.)
    pagespeed EnableFilters rewrite_images;
    #组合 convert_gif_to_png, convert_jpeg_to_progressive, convert_jpeg_to_webp, convert_png_to_jpeg, jpeg_subsampling, recompress_jpeg, recompress_png, recompress_webp, #strip_image_color_profile, and strip_image_meta_data.
    pagespeed EnableFilters recompress_images;
    # 将JPEG图片转化为webp格式
    pagespeed EnableFilters convert_jpeg_to_webp;
    # 将动画Gif图片转化为动画webp格式
    pagespeed EnableFilters convert_to_webp_animated;
    # 图片预加载
    pagespeed EnableFilters inline_preview_images;
    # 移动端图片自适应重置
    pagespeed EnableFilters resize_mobile_images;
    pagespeed EnableFilters responsive_images,resize_images;
    pagespeed EnableFilters insert_image_dimensions;
    pagespeed EnableFilters resize_rendered_image_dimensions;
    pagespeed EnableFilters strip_image_meta_data;
    pagespeed EnableFilters convert_jpeg_to_webp,convert_to_webp_lossless,convert_to_webp_animated;
    pagespeed EnableFilters sprite_images;
    pagespeed EnableFilters convert_png_to_jpeg,convert_jpeg_to_webp;

    # 让JS里引用的图片也加入优化
    pagespeed InPlaceResourceOptimization on;         
    pagespeed EnableFilters in_place_optimize_for_browser;    
    # 不能删 。确保对pagespeed优化资源的请求进入pagespeed处理程序并且没有额外的头部信息
    location ~ "\.pagespeed\.([a-z]\.)?[a-z]{2}\.[^.]{10}\.[^.]+" { add_header "" ""; }
    location ~ "^/pagespeed_static/" { }
    location ~ "^/ngx_pagespeed_beacon$" { }
    location /ngx_pagespeed_statistics { allow 127.0.0.1; deny all; }
    location /ngx_pagespeed_global_statistics { allow 127.0.0.1; deny all; }
    location /ngx_pagespeed_message { allow 127.0.0.1; deny all; }
    location /pagespeed_console { allow 127.0.0.1; deny all; }
    location ~ ^/pagespeed_admin { allow 127.0.0.1; deny all; }
    location ~ ^/pagespeed_global_admin { allow 127.0.0.1; deny all; }

````

docker-compose.yaml
```yml
version: "3.1"
services:
  nginx:
    image: superng6/nginx:debian-stable-1.18.0
    container_name: docker_nginx
    restart: unless-stopped
    network_mode: host
    labels:
      - "docker_nginx"
    volumes:
      - /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro
      - /root/nginx/website:/var/www/html
      - /root/nginx/nginx.conf:/etc/nginx/nginx.conf
      - /root/nginx/conf.d:/etc/nginx/conf.d
      - /root/nginx/ssl:/etc/nginx/ssl
      - /root/nginx/logs:/var/log/nginx
      - /root/nginx/ngx_pagespeed_cache:/var/ngx_pagespeed_cache
```