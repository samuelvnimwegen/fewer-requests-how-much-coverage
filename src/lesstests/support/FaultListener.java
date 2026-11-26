package lesstests;

import io.restassured.RestAssured;
import org.testng.*;

public final class FaultListener implements ISuiteListener, IInvokedMethodListener {
    public void onStart(ISuite suite) {
        RestAssured.filters((request, specification, chain) -> {
            if (!FaultEvidence.selected(Thread.currentThread().getStackTrace())) {
                return chain.next(request, specification);
            }
            request.header(FaultEvidence.HEADER, FaultEvidence.TOKEN);
            var response = chain.next(request, specification);
            if (FaultEvidence.TOKEN.equals(response.header(FaultEvidence.HEADER))) {
                byte[] body = response.asByteArray();
                var headers = new java.util.ArrayList<>(response.headers().asList());
                headers.removeIf(h -> h.getName().equalsIgnoreCase(FaultEvidence.HEADER));
                FaultEvidence.write(FaultEvidence.TOKEN);
                return new io.restassured.builder.ResponseBuilder().clone(response)
                        .setHeaders(new io.restassured.http.Headers(headers)).setBody(body).build();
            }
            return response;
        });
    }

    public void afterInvocation(IInvokedMethod method, ITestResult result) {
        FaultEvidence.finished(result.getThrowable(), method.isTestMethod());
    }
}
