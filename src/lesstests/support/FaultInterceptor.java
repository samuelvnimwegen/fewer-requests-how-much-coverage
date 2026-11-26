package lesstests;

import feign.Client;
import feign.Request;
import java.util.HashMap;
import java.util.List;

public final class FaultInterceptor {
    public static Client wrap(Client client) {
        return (request, options) -> {
            if (!FaultEvidence.selected(Thread.currentThread().getStackTrace())) {
                return client.execute(request, options);
            }
            var tagged = new HashMap<>(request.headers());
            tagged.put(FaultEvidence.HEADER, List.of(FaultEvidence.TOKEN));
            var copy = Request.create(request.httpMethod(), request.url(), tagged, request.body(),
                    request.charset(), request.requestTemplate());
            var response = client.execute(copy, options);
            if (!response.headers().getOrDefault(FaultEvidence.HEADER, List.of()).contains(FaultEvidence.TOKEN)) {
                return response;
            }
            try (response) {
                var headers = new HashMap<>(response.headers());
                headers.keySet().removeIf(FaultEvidence.HEADER::equalsIgnoreCase);
                var result = response.toBuilder().headers(headers);
                if (response.body() != null) {
                    result.body(response.body().asInputStream().readAllBytes());
                }
                FaultEvidence.write(FaultEvidence.TOKEN);
                return result.build();
            }
        };
    }
}
