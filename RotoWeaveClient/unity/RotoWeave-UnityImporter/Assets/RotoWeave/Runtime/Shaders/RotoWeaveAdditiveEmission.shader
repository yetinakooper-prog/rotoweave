Shader "RotoWeave/Sprites/Additive Emission"
{
    Properties
    {
        [PerRendererData] _MainTex ("Emission Texture", 2D) = "black" {}
        _Color ("Tint", Color) = (1,1,1,1)
        [MaterialToggle] PixelSnap ("Pixel snap", Float) = 0
        [HideInInspector] _RendererColor ("Renderer Color", Color) = (1,1,1,1)
        [HideInInspector] _Flip ("Flip", Vector) = (1,1,1,1)
        [PerRendererData] _AlphaTex ("External Alpha", 2D) = "white" {}
        [PerRendererData] _EnableExternalAlpha ("Enable External Alpha", Float) = 0
    }

    SubShader
    {
        Tags
        {
            "Queue"="Transparent"
            "IgnoreProjector"="True"
            "RenderType"="Transparent"
            "PreviewType"="Plane"
            "CanUseSpriteAtlas"="True"
        }
        Cull Off
        Lighting Off
        ZWrite Off
        Blend One One
        ColorMask RGB

        Pass
        {
            CGPROGRAM
            #pragma vertex SpriteVert
            #pragma fragment RotoWeaveEmissionFragment
            #pragma target 2.0
            #pragma multi_compile_instancing
            #pragma multi_compile_local _ PIXELSNAP_ON
            #pragma multi_compile _ ETC1_EXTERNAL_ALPHA
            #include "UnitySprites.cginc"

            fixed4 RotoWeaveEmissionFragment(v2f input) : SV_Target
            {
                fixed4 emission = SampleSpriteTexture(input.texcoord);
                return fixed4(
                    emission.rgb * input.color.rgb * input.color.a,
                    0.0);
            }
            ENDCG
        }
    }

    Fallback Off
}
