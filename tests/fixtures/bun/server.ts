const service_id = process.env.MIM_SERVICE_ID || "local";
const deployment_id = process.env.MIM_DEPLOYMENT_ID || "local";
const body = `<!doctype html><html><head><meta charset="utf-8"><title>Broz Bun</title></head><body><h1>Broz Bun is live</h1><p>${service_id}</p><p>${deployment_id}</p></body></html>`;
Bun.serve({hostname:"0.0.0.0",port:Number(process.env.PORT || 8080),fetch(req){
  const path = new URL(req.url).pathname;
  if(path === "/") return new Response(body,{headers:{"content-type":"text/html; charset=utf-8"}});
  if(path === "/healthz" || path === "/api/status") return Response.json({ok:true,service_id,deployment_id,runtime:"bun"});
  return new Response("not found",{status:404});
}});
