# Docker Nginx

## 简介
### 两个版本
#### 基于官方nginx alpine镜像编译
添加`ngx_cache_purge`、`ngx_brotli`等第三方模块  
添加`www-data:www-data`，`33:33`用户、组，方便Wordpress用户使用  

#### 基于官方nginx debian:buster-slim 镜像编译
添加`ngx_cache_purge`、`ngx_brotli`等第三方模块  

### 注意：请在自己的conf文件中加载模块

````
load_module /usr/local/nginx/modules/ngx_http_brotli_filter_module.so;  
load_module /usr/local/nginx/modules/ngx_http_brotli_static_module.so;  
load_module /usr/local/nginx/modules/ngx_http_cache_purge_module.so;  
````

### brotli 配置文件

````
    brotli on;
    brotli_comp_level 6; 
    brotli_types
        text/css
        text/plain
        text/javascript
        application/javascript
        application/json
        application/x-javascript
        application/xml
        application/xml+rss
        application/xhtml+xml
        application/x-font-ttf
        application/x-font-opentype
        application/vnd.ms-fontobject
        image/svg+xml
        image/x-icon
        application/rss+xml
        application/atom_xml
        image/jpeg
        image/gif
        image/png
        image/icon
        image/bmp
        image/jpg;
````